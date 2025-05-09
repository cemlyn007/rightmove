import datetime
import gzip
import http
import http.client
import json
import urllib.parse
import zoneinfo
from typing import Any, Optional, Union

from tfl import models

TIMEZONE = zoneinfo.ZoneInfo("Europe/London")


class HTTPError(Exception): ...


class RateLimitError(HTTPError):
    def __init__(self, wait: int) -> None:
        self.wait = wait


class NotFoundError(HTTPError): ...


class InternalServerError(HTTPError): ...


class BadGatewayError(HTTPError): ...


class Tfl:
    def __init__(
        self,
        app_key: str,
    ) -> None:
        self._app_key = app_key

    def __call__(
        self,
        from_location: Union[tuple[float, float], str],
        to_location: tuple[float, float],
        arrival_datetime: Optional[datetime.datetime] = None,
    ) -> list[models.Journey]:
        response = get_journey_options(
            from_location,
            to_location,
            arrival_datetime,
            app_key=self._app_key,
        )
        if not response:
            return []
        raw_journeys = response["journeys"]
        journeys = [
            parse_journey(raw_journey, datetime.timezone.utc)
            for raw_journey in raw_journeys
        ]
        if arrival_datetime is None:
            journeys.sort(key=lambda journey: journey.arrival_datetime.timestamp())
        else:
            journeys.sort(
                key=lambda journey: (
                    abs(
                        arrival_datetime.timestamp()
                        - journey.departure_datetime.timestamp()
                    )
                )
            )
        return journeys


def get_journey_options(
    from_location: Union[tuple[float, float], str],
    to_location: tuple[float, float],
    arrival_datetime: Optional[datetime.datetime],
    app_key: str,
) -> Any:
    from_location_encoded = urllib.parse.quote(
        from_location
        if isinstance(from_location, str)
        else ",".join(map(str, from_location))
    )
    to_location_encoded = urllib.parse.quote(",".join(map(str, to_location)))
    url = f"/Journey/JourneyResults/{from_location_encoded}/to/{to_location_encoded}"
    parameters = {
        "app_key": app_key,
        "mode": ",".join(mode.value for mode in models.Mode),
    }
    if arrival_datetime is None:
        departure_datetime = datetime.datetime.now(
            tz=zoneinfo.ZoneInfo("Europe/London")
        )
        date = departure_datetime.strftime("%Y%m%d")
        time = departure_datetime.strftime("%H%M")
        parameters["date"] = date
        parameters["time"] = time
        parameters["timeIs"] = "departing"
    else:
        arrival_datetime = arrival_datetime.astimezone(datetime.timezone.utc)
        date = arrival_datetime.strftime("%Y%m%d")
        time = arrival_datetime.strftime("%H%M")
        parameters["date"] = date
        parameters["time"] = time
        parameters["timeIs"] = "arriving"

    connection = http.client.HTTPSConnection("api.tfl.gov.uk", port=443)
    try:
        response = _request(connection, "GET", url, parameters)
    finally:
        connection.close()
    return response


def parse_journey(
    journey: dict[str, Any], time_zone: datetime.timezone
) -> models.Journey:
    duration_minutes = int(journey["duration"])
    departure_datetime = datetime.datetime.strptime(
        journey["startDateTime"], "%Y-%m-%dT%H:%M:%S"
    ).replace(tzinfo=time_zone)
    arrival_datetime = datetime.datetime.strptime(
        journey["arrivalDateTime"], "%Y-%m-%dT%H:%M:%S"
    ).replace(tzinfo=time_zone)
    modes = [models.Mode(leg["mode"]["id"]) for leg in journey["legs"]]
    if models.Mode.TUBE in modes:
        mode = models.Mode.TUBE
    elif models.Mode.BUS in modes:
        mode = models.Mode.BUS
    else:
        mode = models.Mode.WALKING
    route_names = []
    for leg in journey["legs"]:
        if "routeOptions" in leg:
            name = next(option["name"] for option in leg["routeOptions"])
            if name:
                route_names.append(name)
    route_name = "->".join(route_names) or "walking"
    return models.Journey.model_validate(
        dict(
            duration=datetime.timedelta(minutes=duration_minutes),
            departure_datetime=departure_datetime,
            arrival_datetime=arrival_datetime,
            mode=mode,
            route_name=route_name,
        ),
        strict=True,
    )


def get_next_datetime(
    arrival_time: datetime.time, timezone: datetime.tzinfo = TIMEZONE
) -> datetime.datetime:
    next_day = datetime.datetime.now(tz=timezone).date() + datetime.timedelta(days=1)
    while next_day.weekday() > 4:
        next_day = next_day + datetime.timedelta(days=1)
    return datetime.datetime(
        next_day.year,
        next_day.month,
        next_day.day,
        hour=arrival_time.hour,
        minute=arrival_time.minute,
        second=arrival_time.second,
        microsecond=arrival_time.microsecond,
        tzinfo=timezone,
    )


def _request(
    connection: http.client.HTTPSConnection,
    method: str,
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
    raw_response = http_response.read()
    if http_response.getheader("Content-Encoding") == "gzip":
        raw_response = gzip.decompress(raw_response)
    if raw_response:
        response = json.loads(raw_response)
    else:
        response = None
    if http_response.status == http.HTTPStatus.TOO_MANY_REQUESTS:
        raise RateLimitError(wait=int(http_response.getheader("Retry-After") or -1))
    elif http_response.status == http.HTTPStatus.NOT_FOUND:
        if response and response["message"] == "No journey found for your inputs.":
            return None
        # else...
        raise NotFoundError(http_response.reason)
    elif http_response.status == http.HTTPStatus.INTERNAL_SERVER_ERROR:
        raise InternalServerError(http_response.reason)
    elif http_response.status == http.HTTPStatus.BAD_GATEWAY:
        raise BadGatewayError(http_response.reason)
    elif http_response.status != http.HTTPStatus.OK:
        raise HTTPError(f"HTTP error {http_response.status}: {http_response.reason}")
    return response
