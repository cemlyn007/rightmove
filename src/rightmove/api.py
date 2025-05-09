import copy
import enum
import gzip
import http
import http.client
import json
import urllib.parse
from collections.abc import Iterable, Sequence
from typing import Any, Literal, Optional

import polyline as _polyline
import pydantic
from tenacity import Retrying

from rightmove import models

__all__ = [
    "SEARCH_LIST_MAX_RESULTS",
    "SEARCH_MAP_MAX_RESULTS",
    "SEARCH_BY_IDS_MAX_RESULTS",
    "HTTPError",
    "SortType",
    "MustHave",
    "DontShow",
    "FurnishType",
    "PropertyType",
    "SearchQuery",
    "Rightmove",
    "polyline_identifier",
    "property_url",
]


SEARCH_LIST_MAX_RESULTS = 1000
"The maximum number of results the LIST viewType API will return indices up to."

SEARCH_MAP_MAX_RESULTS = 499
"The maximum number of results the MAP viewType API will return up to."

SEARCH_BY_IDS_MAX_RESULTS = 25
"The maximum number of results the by IDs API will return up to."


class HTTPError(Exception): ...


class SortType(enum.IntEnum):
    """Sort type for search results."""

    LOWEST_PRICE = 1
    HIGHEST_PRICE = 2
    NEAREST_FIRST = 4
    MOST_RECENT = 6
    OLDEST_LISTED = 10


class MustHave(enum.Enum):
    """Must have property features."""

    GARDEN = "garden"
    PARKING = "parking"


class DontShow(enum.Enum):
    """Property types to exclude from search results."""

    HOUSE_SHARE = "houseShare"
    RETIREMENT = "retirement"
    STUDENT = "student"


class FurnishType(enum.Enum):
    """Furnish type for properties."""

    FURNISHED = "furnished"
    PART_FURNISHED = "partFurnished"
    UNFURNISHED = "unfurnished"


class PropertyType(enum.Enum):
    """Property types for search results."""

    FLAT = "flat"
    LAND = "land"
    PARK_HOME = "park-home"
    PRIVATE_HALLS = "private-halls"
    DETACHED = "detached"
    SEMI_DETACHED = "semi-detached"
    TERRACED = "terraced"


class SearchQuery(pydantic.BaseModel):
    location_identifier: str
    min_bedrooms: int = 1
    max_bedrooms: int = 10
    min_price: int = 0
    max_price: Optional[int] = None
    min_bathrooms: int = 1
    max_bathrooms: int = 5
    number_of_properties_per_page: int = pydantic.Field(gt=0, le=25, default=24)
    radius: float = pydantic.Field(gt=-1, default=0)
    "In Miles. Set to 0 to only return properties in area."
    sort_type: SortType = SortType.NEAREST_FIRST
    must_have: Sequence[MustHave] = ()
    dont_show: Sequence[DontShow] = pydantic.Field(
        default=(
            DontShow.HOUSE_SHARE,
            DontShow.RETIREMENT,
            DontShow.STUDENT,
        )
    )
    furnish_types: Sequence[FurnishType] = pydantic.Field(
        default=(
            FurnishType.FURNISHED,
            FurnishType.PART_FURNISHED,
            FurnishType.UNFURNISHED,
        )
    )
    property_types: Sequence[PropertyType] = pydantic.Field(
        default=(
            PropertyType.FLAT,
            PropertyType.DETACHED,
            PropertyType.SEMI_DETACHED,
            PropertyType.TERRACED,
        )
    )
    is_fetching: bool
    max_days_since_added: Optional[int] = None
    channel: Literal["RENT", "BUY"] = "RENT"
    view_type: Literal["LIST", "MAP"] = "LIST"
    area_size_unit: Literal["sqm"] = "sqm"
    currency_code: Literal["GBP"] = "GBP"
    include_let_agreed: bool = False


def polyline_identifier(polyline: list[tuple[float, float]]) -> str:
    return "USERDEFINEDAREA^" + json.dumps(
        {"polylines": _polyline.encode(polyline)}, separators=(", ", ":")
    )


class Rightmove:
    def __init__(self, retrying: Optional[Retrying] = None) -> None:
        self._raw_api = _RawRightmove()
        if retrying is not None:
            self._raw_api.lookup = retrying.wraps(self._raw_api.lookup)
            self._raw_api.search = retrying.wraps(self._raw_api.search)
            self._raw_api.by_ids = retrying.wraps(self._raw_api.by_ids)

    def lookup(
        self,
        query: str,
        limit: Optional[int] = None,
    ) -> models.LookupMatches:
        """Get the location IDs related to a search query.

        Args:
            query (str): Search location query.
            limit (int): Limit, defaulting to the API max limit.

        Returns:
            models.LookupMatches: Matches
        """
        lookup_results = self._raw_api.lookup(query=query, limit=limit)
        return models.LookupMatches.model_validate(lookup_results)

    def search(
        self,
        query: SearchQuery,
    ) -> list[models.Property]:
        """Search for properties using the provided configuration.

        Args:
            query (SearchQuery): Search configuration parameters

        Returns:
            list[models.Property]: List of properties matching the search criteria
                of up to a max length of 1000.
        """
        query = query.model_copy(update={"view_type": "LIST"})
        search_results = self._raw_api.search(query=query)
        return [
            models.Property.model_validate(property)
            for property in search_results["properties"]
        ]

    def map_search(
        self,
        query: SearchQuery,
    ) -> tuple[list[models.PropertyLocation], int]:
        """Search for properties using the provided configuration.

        Args:
            query (SearchQuery): Search configuration parameters

        Returns:
            list[models.PropertyLocation]: List of properties matching the search criteria
                of up to a max length of 499.
            int: Number of properties matching the search criteria.
        """
        query = query.model_copy(update={"view_type": "MAP"})
        location_results = self._raw_api.search(query=query)
        return [
            models.PropertyLocation.model_validate(property)
            for property in location_results["properties"]
        ], int(location_results["resultCount"].replace(",", ""))

    def search_by_ids(
        self,
        ids: Iterable[int],
        channel: Literal["RENT", "BUY"],
    ) -> list[models.Property]:
        "Note that only 25 ids can be passed at a time."
        search_results = self._raw_api.by_ids(ids=ids, channel=channel)
        return [
            models.Property.model_validate(property)
            for property in search_results["properties"]
        ]


def property_url(property_url: str) -> str:
    return f"https://{_RawRightmove.BASE_HOST}{property_url}"


class _RawRightmove:
    BASE_HOST = "www.rightmove.co.uk"
    LOS_HOST = "los.rightmove.co.uk"
    LOS_LIMIT = 20
    "The maximum search results the lookup service will return."

    def lookup(self, query: str, limit: Optional[int] = None) -> dict[str, Any]:
        """Get the location IDs related to a search query.

        Args:
            query (str): Search location query.
            limit (int): Limit, defaulting to the API max limit.

        Returns:
            dict[str, Any]: Matches
        """
        connection = http.client.HTTPSConnection(self.LOS_HOST, port=443)
        try:
            return self._request(
                connection,
                "GET",
                "/typeahead",
                {
                    "query": query,
                    "limit": limit or self.LOS_LIMIT,
                    "exclude": "",
                },
            )
        finally:
            connection.close()

    def search(
        self,
        query: SearchQuery,
    ) -> dict[str, Any]:
        params = self._get_search_params(query)
        return self._search(params)

    def by_ids(
        self,
        ids: Iterable[int],
        channel: Literal["RENT", "BUY"],
    ) -> dict[str, Any]:
        params = {
            "channel": channel,
            "propertyIds": ",".join(map(str, ids)),
            "viewType": "MAP",
        }
        connection = http.client.HTTPSConnection(self.BASE_HOST, port=443)
        try:
            return self._request(
                connection,
                "GET",
                "/api/_searchByIds",
                params,
            )
        finally:
            connection.close()

    def property_url(self, property_url: str) -> str:
        return f"https://{self.BASE_HOST}{property_url}"

    def _get_search_params(self, query: SearchQuery) -> dict[str, Any]:
        params = {
            "locationIdentifier": query.location_identifier,
            "numberOfPropertiesPerPage": query.number_of_properties_per_page,
            "radius": query.radius,
            "sortType": query.sort_type.value,
            "includeLetAgreed": query.include_let_agreed,
            "viewType": query.view_type,
            "channel": query.channel,
            "areaSizeUnit": query.area_size_unit,
            "currencyCode": query.currency_code,
            "isFetching": query.is_fetching,
        }
        if query.min_price:
            params["minPrice"] = query.min_price
        if query.max_price:
            params["maxPrice"] = query.max_price
        if query.dont_show:
            params["dontShow"] = ",".join(
                dont_show.value for dont_show in query.dont_show
            )
        if query.furnish_types:
            params["furnishTypes"] = ",".join(
                furnish_type.value for furnish_type in query.furnish_types
            )
        if query.must_have:
            params["mustHave"] = ",".join(
                must_have.value for must_have in query.must_have
            )
        if query.property_types:
            params["propertyTypes"] = ",".join(
                property_type.value for property_type in query.property_types
            )
        if query.include_let_agreed:
            params["_includeLetAgreed"] = "on"
        if query.max_days_since_added is not None:
            params["maxDaysSinceAdded"] = query.max_days_since_added
        if query.min_bedrooms:
            params["minBedrooms"] = query.min_bedrooms
        if query.max_bedrooms:
            params["maxBedrooms"] = query.max_bedrooms
        if query.min_bathrooms:
            params["minBathrooms"] = query.min_bathrooms
        if query.max_bathrooms:
            params["maxBathrooms"] = query.max_bathrooms
        return params

    def _search(self, params: dict[str, Any]) -> dict[str, Any]:
        connection = http.client.HTTPSConnection(self.BASE_HOST, port=443)
        try:
            endpoint_url = {
                "LIST": "/api/_search",
                "MAP": "/api/_mapSearch",
            }[params["viewType"]]
            response = self._request(
                connection,
                "GET",
                endpoint_url,
                params,
            )
            # MAP doesn't support pagination, so you'll
            #  only get the first page of results.
            if params["viewType"] == "MAP":
                return response
            full_response = copy.deepcopy(response)
            while len(full_response["properties"]) < min(
                int(response["resultCount"].replace(",", "")), SEARCH_LIST_MAX_RESULTS
            ):
                params = copy.deepcopy(params)
                params["index"] = int(response["pagination"]["next"])
                response = self._request(
                    connection,
                    "GET",
                    endpoint_url,
                    params,
                )
                full_response["properties"].extend(response["properties"])
            return full_response
        finally:
            connection.close()

    def _request(
        self,
        connection: http.client.HTTPSConnection,
        method: Literal["GET"],
        url: str,
        parameters: dict[str, Any],
    ) -> Any:
        query_string = urllib.parse.urlencode(parameters, doseq=True)
        urlparse = urllib.parse.urlparse(url)
        urlparse = urlparse._replace(query=query_string)
        url = urllib.parse.urlunparse(urlparse)
        connection.request(
            method,
            url,
            headers={
                "User-Agent": "IAmLookingToRent/0.0.0",
                "Accept-Encoding": "gzip",
                "Accept": "*/*",
                "Connection": "keep-alive",
            },
        )
        http_response = connection.getresponse()
        if http_response.status != http.HTTPStatus.OK:
            raise HTTPError(
                f"HTTP error {http_response.status}: {http_response.reason}"
            )
        raw_response = http_response.read()
        if http_response.getheader("Content-Encoding") == "gzip":
            raw_response = gzip.decompress(raw_response)
        response = json.loads(raw_response)
        return response
