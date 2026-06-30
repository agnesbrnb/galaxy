import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from typing import (
    Any,
    Optional,
    Union,
)

from galaxy import exceptions
from galaxy.files.models import (
    AnyRemoteEntry,
    BaseFileSourceConfiguration,
    BaseFileSourceTemplateConfiguration,
    FilesSourceRuntimeContext,
    RemoteDirectory,
    RemoteFile,
)
from galaxy.util.config_templates import TemplateExpansion
from . import BaseFilesSource

DEFAULT_BASE_URL = "https://siduri.migale.inrae.fr"

# OpenSILEX serves its REST API under this prefix (e.g. https://host/rest/core/projects).
API_PREFIX = "/rest"

# OpenSILEX authenticates protected endpoints with an "Authorization: Bearer <token>" header.
# See ApiProtected.HEADER_NAME / TOKEN_PARAMETER_PREFIX in the OpenSILEX source.
TOKEN_PREFIX = "Bearer "

# Page size used when walking through paginated OpenSILEX listings.
PAGE_SIZE = 100


class OpenSILEXFileSourceTemplateConfiguration(BaseFileSourceTemplateConfiguration):
    base_url: Union[str, TemplateExpansion] = DEFAULT_BASE_URL
    api_key: Union[str, TemplateExpansion]


class OpenSILEXFileSourceConfiguration(BaseFileSourceConfiguration):
    base_url: str = DEFAULT_BASE_URL
    api_key: str


class OpenSILEXFilesSource(BaseFilesSource[OpenSILEXFileSourceTemplateConfiguration, OpenSILEXFileSourceConfiguration]):
    """Browse an OpenSILEX instance as projects -> experiments -> datafiles.

    The navigation hierarchy is:
      * "/"                                  -> list projects (directories)
      * "/{project}"                         -> list experiments of that project (directories)
      * "/{project}/{experiment}"            -> list datafiles of that experiment (files)
      * "/{project}/{experiment}/{datafile}" -> a downloadable datafile

    OpenSILEX object identifiers are themselves URIs (they contain "/" and ":"), so each
    identifier is URL-encoded into a single path segment.
    """

    plugin_type = "opensilex"
    supports_pagination = False
    supports_search = True

    template_config_class = OpenSILEXFileSourceTemplateConfiguration
    resolved_config_class = OpenSILEXFileSourceConfiguration

    # --- Browsing -----------------------------------------------------------------

    def _list(
        self,
        context: FilesSourceRuntimeContext[OpenSILEXFileSourceConfiguration],
        path="/",
        recursive=False,
        write_intent: bool = False,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        query: Optional[str] = None,
        sort_by: Optional[str] = None,
    ) -> tuple[list[AnyRemoteEntry], int]:
        config = context.config
        segments = self._path_segments(path)

        if len(segments) == 0:
            entries = self._list_projects(config, query)
        elif len(segments) == 1:
            entries = self._list_experiments(config, segments[0], query)
        elif len(segments) == 2:
            entries = self._list_datafiles(config, segments[0], segments[1], query)
        else:
            raise exceptions.ObjectNotFound(f"Cannot list contents of a datafile [{path}].")

        return entries, len(entries)

    def _list_projects(self, config: OpenSILEXFileSourceConfiguration, query: Optional[str]) -> list[AnyRemoteEntry]:
        params = {"name": query} if query else {}
        projects = self._get_all_results(config, "/core/projects", params)
        entries: list[AnyRemoteEntry] = []
        for project in projects:
            uri = project["uri"]
            rel_path = self._encode(uri)
            name = project.get("name") or project.get("shortname") or uri
            entries.append(RemoteDirectory(name=name, uri=self.uri_from_path(rel_path), path=rel_path))
        return entries

    def _list_experiments(
        self, config: OpenSILEXFileSourceConfiguration, project_uri: str, query: Optional[str]
    ) -> list[AnyRemoteEntry]:
        params: dict[str, Any] = {"projects": project_uri}
        if query:
            params["name"] = query
        experiments = self._get_all_results(config, "/core/experiments", params)
        parent = self._encode(project_uri)
        entries: list[AnyRemoteEntry] = []
        for experiment in experiments:
            uri = experiment["uri"]
            rel_path = f"{parent}/{self._encode(uri)}"
            name = experiment.get("name") or uri
            entries.append(RemoteDirectory(name=name, uri=self.uri_from_path(rel_path), path=rel_path))
        return entries

    def _list_datafiles(
        self, config: OpenSILEXFileSourceConfiguration, project_uri: str, experiment_uri: str, query: Optional[str]
    ) -> list[AnyRemoteEntry]:
        params: dict[str, Any] = {"experiments": experiment_uri}
        if query:
            params["name"] = query
        datafiles = self._get_all_results(config, "/core/datafiles/by_targets", params)
        parent = f"{self._encode(project_uri)}/{self._encode(experiment_uri)}"
        entries: list[AnyRemoteEntry] = []
        for datafile in datafiles:
            uri = datafile["uri"]
            rel_path = f"{parent}/{self._encode(uri)}"
            name = datafile.get("filename") or uri.rsplit("/", 1)[-1]
            entries.append(
                RemoteFile(
                    name=name,
                    uri=self.uri_from_path(rel_path),
                    path=rel_path,
                    ctime=datafile.get("date"),
                )
            )
        return entries

    # --- Download -----------------------------------------------------------------

    def _realize_to(
        self,
        source_path: str,
        native_path: str,
        context: FilesSourceRuntimeContext[OpenSILEXFileSourceConfiguration],
    ):
        segments = self._path_segments(source_path)
        if not segments:
            raise exceptions.ObjectNotFound(f"Could not determine a datafile to download from [{source_path}].")
        datafile_uri = segments[-1]
        url = self._api_url(context.config, f"/core/datafiles/{self._encode(datafile_uri)}")
        req = urllib.request.Request(url, headers=self._headers(context.config))
        try:
            with urllib.request.urlopen(req) as response, open(native_path, "wb") as out:
                shutil.copyfileobj(response, out)
        except urllib.error.HTTPError as e:
            raise exceptions.MessageException(self._http_error_message("download datafile", e))

    def _write_from(
        self,
        target_path: str,
        native_path: str,
        context: FilesSourceRuntimeContext[OpenSILEXFileSourceConfiguration],
    ) -> Optional[str]:
        # This is a read-only file source: writing back to OpenSILEX is not supported.
        raise NotImplementedError("Writing to an OpenSILEX file source is not supported.")

    # --- HTTP helpers -------------------------------------------------------------

    def _api_url(
        self, config: OpenSILEXFileSourceConfiguration, path: str, params: Optional[dict[str, Any]] = None
    ) -> str:
        base = config.base_url.rstrip("/")
        url = f"{base}{API_PREFIX}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"
        return url

    def _headers(self, config: OpenSILEXFileSourceConfiguration) -> dict[str, str]:
        return {
            "Authorization": f"{TOKEN_PREFIX}{config.api_key}",
            "Accept": "application/json",
        }

    def _api_get_json(
        self, config: OpenSILEXFileSourceConfiguration, path: str, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        url = self._api_url(config, path, params)
        req = urllib.request.Request(url, headers=self._headers(config))
        try:
            with urllib.request.urlopen(req) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            raise exceptions.MessageException(self._http_error_message(f"GET {path}", e))

    def _get_all_results(
        self, config: OpenSILEXFileSourceConfiguration, path: str, params: Optional[dict[str, Any]] = None
    ) -> list[dict[str, Any]]:
        """Walk through all pages of an OpenSILEX listing and return the flattened results."""
        params = dict(params or {})
        page = 0
        results: list[dict[str, Any]] = []
        while True:
            params["page"] = page
            params["page_size"] = PAGE_SIZE
            payload = self._api_get_json(config, path, params)
            results.extend(payload.get("result", []))
            pagination = (payload.get("metadata") or {}).get("pagination") or {}
            total_pages = pagination.get("totalPages") or 0
            if page >= total_pages - 1:
                break
            page += 1
        return results

    @staticmethod
    def _http_error_message(action: str, error: urllib.error.HTTPError) -> str:
        body = error.read().decode("utf-8", errors="replace")
        if error.code == 401:
            return f"Failed to {action}: unauthorized (401). Check the OpenSILEX api_key/token. {body}"
        return f"Failed to {action}: OpenSILEX returned {error.code}. {body}"

    # --- Path encoding ------------------------------------------------------------

    @staticmethod
    def _encode(uri: str) -> str:
        """Encode an OpenSILEX URI so it can be used as a single path segment."""
        return urllib.parse.quote(uri, safe="")

    def _path_segments(self, path: str) -> list[str]:
        """Split an incoming path into decoded OpenSILEX URIs.

        Accepts either a relative path (e.g. "/{project}/{experiment}") or a full
        gxfiles:// URI; the uri root is stripped if present.
        """
        if not path:
            return []
        root = self.get_uri_root()
        if path.startswith(root):
            path = path[len(root) :]
        path = path.strip("/")
        if not path:
            return []
        return [urllib.parse.unquote(segment) for segment in path.split("/")]


__all__ = ("OpenSILEXFilesSource",)
