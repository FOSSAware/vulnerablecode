# Copyright (c)  nexB Inc. and others. All rights reserved.
# http://nexb.com and https://github.com/nexB/vulnerablecode/
# The VulnerableCode software is licensed under the Apache License version 2.0.
# Data generated with VulnerableCode require an acknowledgment.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
# When you publish or redistribute any data created with VulnerableCode or any VulnerableCode
# derivative work, you must accompany this data with the following acknowledgment:
#
#  Generated with VulnerableCode and provided on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, either express or implied. No content created from
#  VulnerableCode should be considered or used as legal advice. Consult an Attorney
#  for any legal advice.
#  VulnerableCode is a free software  from nexB Inc. and others.
#  Visit https://github.com/nexB/vulnerablecode/ for support and download.

import logging
from datetime import datetime
from typing import Iterable
from typing import List
from typing import Mapping
from typing import Optional
from typing import Tuple

from dateutil import parser as dateparser
from django.db.models.query import QuerySet
from packageurl import PackageURL
from univers.version_range import VersionRange
from univers.version_range import build_range_from_github_advisory_constraint

from vulnerabilities import helpers
from vulnerabilities import severity_systems
from vulnerabilities.helpers import AffectedPackage as LegacyAffectedPackage
from vulnerabilities.helpers import get_item
from vulnerabilities.helpers import nearest_patched_package
from vulnerabilities.importer import AdvisoryData
from vulnerabilities.importer import AffectedPackage
from vulnerabilities.importer import Importer
from vulnerabilities.importer import Reference
from vulnerabilities.importer import UnMergeablePackageError
from vulnerabilities.importer import VulnerabilitySeverity
from vulnerabilities.improver import Improver
from vulnerabilities.improver import Inference
from vulnerabilities.models import Advisory
from vulnerabilities.package_managers import ComposerVersionAPI
from vulnerabilities.package_managers import GoproxyVersionAPI
from vulnerabilities.package_managers import MavenVersionAPI
from vulnerabilities.package_managers import NugetVersionAPI
from vulnerabilities.package_managers import PypiVersionAPI
from vulnerabilities.package_managers import RubyVersionAPI
from vulnerabilities.package_managers import VersionAPI

logger = logging.getLogger(__name__)

WEIRD_IGNORABLE_VERSIONS = frozenset(
    [
        "0.1-bulbasaur",
        "0.1-charmander",
        "0.3m1",
        "0.3m2",
        "0.3m3",
        "0.3m4",
        "0.3m5",
        "0.4m1",
        "0.4m2",
        "0.4m3",
        "0.4m4",
        "0.4m5",
        "0.5m1",
        "0.5m2",
        "0.5m3",
        "0.5m4",
        "0.5m5",
        "0.6m1",
        "0.6m2",
        "0.6m3",
        "0.6m4",
        "0.6m5",
        "0.6m6",
        "0.7.10p1",
        "0.7.11p1",
        "0.7.11p2",
        "0.7.11p3",
        "0.8.1p1",
        "0.8.3p1",
        "0.8.4p1",
        "0.8.4p2",
        "0.8.6p1",
        "0.8.7p1",
        "0.9-doduo",
        "0.9-eevee",
        "0.9-fearow",
        "0.9-gyarados",
        "0.9-horsea",
        "0.9-ivysaur",
        "2013-01-21T20:33:09+0100",
        "2013-01-23T17:11:52+0100",
        "2013-02-01T20:50:46+0100",
        "2013-02-02T19:59:03+0100",
        "2013-02-02T20:23:17+0100",
        "2013-02-08T17:40:57+0000",
        "2013-03-27T16:32:26+0100",
        "2013-05-09T12:47:53+0200",
        "2013-05-10T17:55:56+0200",
        "2013-05-14T20:16:05+0200",
        "2013-06-01T10:32:51+0200",
        "2013-07-19T09:11:08+0000",
        "2013-08-12T21:48:56+0200",
        "2013-09-11T19-27-10",
        "2013-12-23T17-51-15",
        "2014-01-12T15-52-10",
        "2.0.1rc2-git",
        "3.0.0b3-",
        "3.0b6dev-r41684",
        "-class.-jw.util.version.Version-",
    ]
)

PACKAGE_TYPE_BY_GITHUB_ECOSYSTEM = {
    "MAVEN": "maven",
    "NUGET": "nuget",
    "COMPOSER": "composer",
    "PIP": "pypi",
    "RUBYGEMS": "gem",
    "GO": "golang",
}

GITHUB_ECOSYSTEM_BY_PACKAGE_TYPE = {
    value: key for (key, value) in PACKAGE_TYPE_BY_GITHUB_ECOSYSTEM.items()
}

# TODO: We will try to gather more info from GH API
# Check https://github.com/nexB/vulnerablecode/issues/645
# set of all possible values of first '%s' = {'MAVEN','COMPOSER', 'NUGET', 'RUBYGEMS', 'PYPI'}
# second '%s' is interesting, it will have the value '' for the first request,
GRAPHQL_QUERY_TEMPLATE = """
query{
    securityVulnerabilities(first: 100, ecosystem: %s, %s) {
        edges {
            node {
                advisory {
                    identifiers {
                        type
                        value
                    }
                    summary
                    references {
                        url
                    }
                    severity
                    publishedAt
                }
                package {
                    name
                }
                vulnerableVersionRange
            }
        }
        pageInfo {
            hasNextPage
            endCursor
        }
    }
}
"""

VERSION_API_CLASSES = [
    MavenVersionAPI,
    NugetVersionAPI,
    ComposerVersionAPI,
    PypiVersionAPI,
    RubyVersionAPI,
    GoproxyVersionAPI,
]

VERSION_API_CLASSES_BY_PACKAGE_TYPE = {cls.package_type: cls for cls in VERSION_API_CLASSES}


class GitHubAPIImporter(Importer):
    spdx_license_expression = "CC-BY-4.0"

    def advisory_data(self) -> Iterable[AdvisoryData]:
        for ecosystem, package_type in PACKAGE_TYPE_BY_GITHUB_ECOSYSTEM.items():
            end_cursor_exp = ""
            while True:
                graphql_query = {"query": GRAPHQL_QUERY_TEMPLATE % (ecosystem, end_cursor_exp)}
                response = helpers.fetch_github_graphql_query(graphql_query)

                page_info = get_item(response, "data", "securityVulnerabilities", "pageInfo")
                end_cursor = get_item(page_info, "endCursor")
                if end_cursor:
                    end_cursor = f'"{end_cursor}"'
                    end_cursor_exp = f"after: {end_cursor}"

                yield from process_response(response, package_type=package_type)

                if not get_item(page_info, "hasNextPage"):
                    break


def get_reference_id(url: str):
    """
    Return the reference id from a URL
    For example:
    >>> get_reference_id("https://github.com/advisories/GHSA-c9hw-wf7x-jp9j")
    'GHSA-c9hw-wf7x-jp9j'
    """
    url_parts = url.split("/")
    last_url_part = url_parts[-1]
    return last_url_part


def extract_references(reference_data: List[dict]) -> Iterable[Reference]:
    """
    Yield `reference` by iterating over `reference_data`
    >>> list(extract_references([{'url': "https://github.com/advisories/GHSA-c9hw-wf7x-jp9j"}]))
    [Reference(url="https://github.com/advisories/GHSA-c9hw-wf7x-jp9j"), reference_id = "GHSA-c9hw-wf7x-jp9j" ]
    >>> list(extract_references([{'url': "https://github.com/advisories/c9hw-wf7x-jp9j"}]))
    [Reference(url="https://github.com/advisories/c9hw-wf7x-jp9j")]
    """
    for ref in reference_data:
        url = ref["url"]
        if not isinstance(url, str):
            logger.error(f"extract_references: url is not of type `str`: {url}")
            continue
        if "GHSA-" in url.upper():
            reference = Reference(url=url, reference_id=get_reference_id(url))
        else:
            reference = Reference(url=url)
        yield reference


def get_purl(pkg_type: str, github_name: str) -> Optional[PackageURL]:
    """
    Return a PackageURL by splitting the `github_name` using the `pkg_type` convention.
    Return None and log an error if we can not split or it is an unknown package type.
    >>> get_purl("maven", "org.apache.commons:commons-lang3")
    PackageURL(type="maven", namespace="org.apache.commons", name="commons-lang3")
    >>> get_purl("composer", "foo/bar")
    PackageURL(type="composer", namespace="foo", name="bar")
    """
    if pkg_type == "maven":
        if ":" not in github_name:
            logger.error(f"get_purl: Invalid maven package name {github_name}")
            return
        ns, _, name = github_name.partition(":")
        return PackageURL(type=pkg_type, namespace=ns, name=name)

    if pkg_type == "composer":
        if "/" not in github_name:
            logger.error(f"get_purl: Invalid composer package name {github_name}")
            return
        vendor, _, name = github_name.partition("/")
        return PackageURL(type=pkg_type, namespace=vendor, name=name)

    if pkg_type in ("nuget", "pypi", "gem", "golang"):
        return PackageURL(type=pkg_type, name=github_name)

    logger.error(f"get_purl: Unknown package type {pkg_type}")


class InvalidVersionRange(Exception):
    """
    Raises exception when the version range is invalid
    """


def get_api_package_name(purl: PackageURL) -> str:
    """
    Return the package name expected by the GitHub API given a PackageURL
    >>> get_api_package_name(PackageURL(type="maven", namespace="org.apache.commons", name="commons-lang3"))
    "org.apache.commons:commons-lang3"
    >>> get_api_package_name(PackageURL(type="composer", namespace="foo", name="bar"))
    "foo/bar"
    """
    if purl.type == "maven":
        return f"{purl.namespace}:{purl.name}"

    if purl.type == "composer":
        return f"{purl.namespace}/{purl.name}"

    if purl.type in ("nuget", "pypi", "gem", "golang"):
        return purl.name

    logger.error(f"get_api_package_name: Unknown PURL {purl!r}")


def process_response(resp: dict, package_type: str) -> Iterable[AdvisoryData]:
    """
    Yield `AdvisoryData` by taking `resp` and `ecosystem` as input
    """
    vulnerabilities = get_item(resp, "data", "securityVulnerabilities", "edges") or []
    if not vulnerabilities:
        logger.error(
            f"No vulnerabilities found for package_type: {package_type!r} in response: {resp!r}"
        )
        return

    for vulnerability in vulnerabilities:
        affected_packages = []
        aliases = set()
        github_advisory = get_item(vulnerability, "node")
        if not github_advisory:
            logger.error(f"No node found in {vulnerability!r}")
            continue

        name = get_item(github_advisory, "package", "name")
        if not name:
            logger.error(f"No name found in {github_advisory!r}")
            continue

        purl = get_purl(pkg_type=package_type, github_name=name)
        if not purl:
            continue

        vulnerable_range = get_item(github_advisory, "vulnerableVersionRange")
        if not vulnerable_range:
            logger.error(f"No affected range found in {github_advisory!r}")
            continue

        affected_range = None
        try:
            affected_range = build_range_from_github_advisory_constraint(
                package_type, vulnerable_range
            )
        except InvalidVersionRange:
            logger.error(f"Could not parse affected range {vulnerable_range!r}")
            continue

        if affected_range != NotImplementedError:
            affected_packages.append(
                AffectedPackage(
                    package=purl,
                    affected_version_range=affected_range,
                )
            )

        advisory = get_item(github_advisory, "advisory")
        if not advisory:
            logger.error(f"No advisory found in {github_advisory!r}")
            continue

        references = get_item(advisory, "references") or []
        if references:
            references: List[Reference] = list(extract_references(references))

        summary = get_item(advisory, "summary")
        identifiers = get_item(advisory, "identifiers") or []
        for identifier in identifiers:
            value = identifier["value"]
            identifier_type = identifier["type"]
            aliases.add(value)
            # attach the GHSA with severity score
            if identifier_type == "GHSA":
                # Each Node has only one GHSA, hence exit after attaching
                # score to this GHSA
                for ref in references:
                    if ref.reference_id == value:
                        severity = get_item(advisory, "severity")
                        if severity:
                            ref.severities = [
                                VulnerabilitySeverity(
                                    system=severity_systems.CVSS31_QUALITY,
                                    value=severity,
                                )
                            ]

            elif identifier_type == "CVE":
                pass
            else:
                logger.error(f"Unknown identifier type {identifier_type!r} and value {value!r}")

        date_published = get_item(advisory, "publishedAt")
        if date_published:
            date_published = dateparser.parse(date_published)

        yield AdvisoryData(
            aliases=sorted(list(aliases)),
            summary=summary,
            references=references,
            affected_packages=affected_packages,
            date_published=date_published,
        )


class GitHubBasicImprover(Improver):
    def __init__(self) -> None:
        self.versions_fetcher_by_purl: Mapping[str, VersionAPI] = {}

    @property
    def interesting_advisories(self) -> QuerySet:
        return Advisory.objects.filter(created_by=GitHubAPIImporter.qualified_name)

    def get_package_versions(
        self, package_url: PackageURL, until: Optional[datetime] = None
    ) -> List[str]:
        """
        Return a list of `valid_versions` for the `package_url`
        """
        api_name = get_api_package_name(package_url)
        if not api_name:
            logger.error(f"Could not get versions for {package_url!r}")
            return []
        versions_fetcher = self.versions_fetcher_by_purl.get(package_url)
        if not versions_fetcher:
            versions_fetcher: VersionAPI = VERSION_API_CLASSES_BY_PACKAGE_TYPE[package_url.type]
            self.versions_fetcher_by_purl[package_url] = versions_fetcher()

        versions_fetcher = self.versions_fetcher_by_purl[package_url]

        self.versions_fetcher_by_purl[package_url] = versions_fetcher
        return versions_fetcher.get_until(package_name=api_name, until=until).valid_versions

    def get_inferences(self, advisory_data: AdvisoryData) -> Iterable[Inference]:
        """
        Yield Inferences for the given advisory data
        """
        if not advisory_data.affected_packages:
            return
        try:
            purl, affected_version_ranges, _ = AffectedPackage.merge(
                advisory_data.affected_packages
            )
        except UnMergeablePackageError:
            logger.error(f"Cannot merge with different purls {advisory_data.affected_packages!r}")
            return iter([])

        pkg_type = purl.type
        pkg_namespace = purl.namespace
        pkg_name = purl.name
        if purl.type == "golang":
            # Problem with the Golang and Go that they provide full path
            # FIXME: We need to get the PURL subpath for Go module
            versions_fetcher = self.versions_fetcher_by_purl.get(purl)
            if not versions_fetcher:
                versions_fetcher = GoproxyVersionAPI()
                self.versions_fetcher_by_purl[purl] = versions_fetcher
            pkg_name = versions_fetcher.module_name_by_package_name.get(pkg_name, pkg_name)

        valid_versions = self.get_package_versions(
            package_url=purl, until=advisory_data.date_published
        )
        for affected_version_range in affected_version_ranges:
            aff_vers, unaff_vers = resolve_version_range(
                affected_version_range=affected_version_range,
                package_versions=valid_versions,
            )
            affected_purls = [
                PackageURL(type=pkg_type, namespace=pkg_namespace, name=pkg_name, version=version)
                for version in aff_vers
            ]

            unaffected_purls = [
                PackageURL(type=pkg_type, namespace=pkg_namespace, name=pkg_name, version=version)
                for version in unaff_vers
            ]

            affected_packages: List[LegacyAffectedPackage] = nearest_patched_package(
                vulnerable_packages=affected_purls, resolved_packages=unaffected_purls
            )

            unique_patched_packages_with_affected_packages = {}
            for package in affected_packages:
                if package.patched_package not in unique_patched_packages_with_affected_packages:
                    unique_patched_packages_with_affected_packages[package.patched_package] = []
                unique_patched_packages_with_affected_packages[package.patched_package].append(
                    package.vulnerable_package
                )

            for (
                fixed_package,
                affected_packages,
            ) in unique_patched_packages_with_affected_packages.items():
                yield Inference.from_advisory_data(
                    advisory_data,
                    confidence=100,  # We are getting all valid versions to get this inference
                    affected_purls=affected_packages,
                    fixed_purl=fixed_package,
                )


def resolve_version_range(
    affected_version_range: VersionRange,
    package_versions: List[str],
    ignorable_versions=WEIRD_IGNORABLE_VERSIONS,
) -> Tuple[List[str], List[str]]:
    """
    Given an affected version range and a list of `package_versions`, resolve
    which versions are in this range and return a tuple of two lists of
    `affected_versions` and `unaffected_versions`.
    """
    if not affected_version_range:
        logger.error(f"affected version range is {affected_version_range!r}")
        return [], []
    affected_versions = []
    unaffected_versions = []
    for package_version in package_versions or []:
        if package_version in ignorable_versions:
            continue
        # Remove whitespace
        package_version = package_version.replace(" ", "")
        # Remove leading 'v'
        package_version = package_version.lstrip("vV")
        try:
            version = affected_version_range.version_class(package_version)
        except Exception:
            logger.error(f"Could not parse version {package_version!r}")
            continue
        if version in affected_version_range:
            affected_versions.append(package_version)
        else:
            unaffected_versions.append(package_version)
    return affected_versions, unaffected_versions
