import datetime
import logging
import webbrowser
import zoneinfo
from typing import Optional, Union

import flathunt.map
import tfl.api
from rightmove import api, models, property_cache

logger = logging.Logger(__name__)


class App:
    def __init__(
        self,
        commute_coordinates: list[tuple[float, float]],
        cache: Optional[property_cache.PropertyCache],
        tfl_app_key: str,
    ) -> None:
        self._api = api.Rightmove()
        self._cache = cache
        self._commute_coordinates = commute_coordinates
        self._tfl_app_key = tfl_app_key

    def search(
        self,
        location_name: str,
        location_id: str,
        max_price: int,
        max_miles_radius: float,
        max_days_since_added: Optional[int],
        journey_coordinates: dict[str, tuple[float, float]],
        max_journey_timedelta: datetime.timedelta,
    ) -> None:
        try:
            if location_id.startswith("STATION^") and not self._check_journey(
                location=location_name,
                journey_coordinates=journey_coordinates,
                max_journey_timedelta=max_journey_timedelta,
            ):
                return
        except tfl.api.HTTPError:
            logger.exception('Location name "%s"')

        query = api.SearchQuery(
            location_identifier=location_id,
            min_bedrooms=1,
            min_price=0,
            max_price=max_price,
            number_of_properties_per_page=24,
            radius=max_miles_radius,
            sort_type=api.SortType.MOST_RECENT,
            include_let_agreed=False,
            is_fetching=True,
            max_days_since_added=max_days_since_added,
        )
        properties = self._api.search(query)
        logger.info("Search returned %d properties", len(properties))
        new_properties = [
            property
            for property in properties
            if not self._cache or not self._cache.contains_property_id(property.id)
        ]
        logger.info(
            "After filtering cached properties, returned %d properties",
            len(new_properties),
        )
        for index, property in enumerate(new_properties):
            if not property.price:
                logger.info('Skipping "%s" because it has no price!')
                continue

            logger.info('Checking journey from "%s"', property.display_address)
            if not self._check_journey(
                location=(property.location.latitude, property.location.longitude),
                journey_coordinates=journey_coordinates,
                max_journey_timedelta=max_journey_timedelta,
            ):
                if self._cache:
                    self._cache.add(property.model_dump(mode="json"))
                logger.info(
                    'Skipping "%s" (%s %s)',
                    property.display_address,
                    property.price.amount,
                    property.price.frequency,
                )
                continue
            logger.info(
                'Showing "%s" (%s %s)',
                property.display_address,
                property.price.amount,
                property.price.frequency,
            )
            self._show(property)
            if index != len(new_properties) - 1:
                self._wait("Press enter for next property...")

            if self._cache:
                self._cache.add(property.model_dump(mode="json"))

    def _check_journey(
        self,
        location: Union[tuple[float, float], str],
        journey_coordinates: dict[str, tuple[float, float]],
        max_journey_timedelta: datetime.timedelta,
    ) -> bool:
        tzinfo = zoneinfo.ZoneInfo("Europe/London")
        arrival_datetime = tfl.api.get_next_datetime(
            datetime.time(9, 0, 0, 0, tzinfo=tzinfo)
        )
        tfl_api = tfl.api.Tfl(
            app_key=self._tfl_app_key,
        )
        for location_name, journey_coordinate in journey_coordinates.items():
            journeys = tfl_api(
                from_location=location,
                to_location=journey_coordinate,
                arrival_datetime=arrival_datetime,
            )
            logger.info("Location: %s, Journey: %d", location_name, len(journeys))
            min_journey = min(
                journeys,
                key=lambda journey: arrival_datetime - journey.departure_datetime,
            )
            min_journey_timedelta = arrival_datetime - min_journey.departure_datetime
            if min_journey_timedelta > max_journey_timedelta:
                logger.info(
                    "Unacceptable journeys (%s, %s)",
                    location_name,
                    min_journey_timedelta,
                )
                return False
            logger.info(
                "Acceptable journey (%s, %s, %s)",
                location_name,
                min_journey.mode.value,
                min_journey_timedelta,
            )
        logger.info("Acceptable journeys")
        return True

    def _show(self, property: models.Property) -> None:
        self._show_advert(property)
        if not self._commute_coordinates or self._skip():
            return
        # else...
        for index, commute_coordinate in enumerate(self._commute_coordinates):
            self._show_route(property, commute_coordinate)
            if index != len(self._commute_coordinates) - 1:
                self._wait("Press enter for next commute location...")

    def _show_advert(self, property: models.Property) -> None:
        # Some properties don't have a URL.
        if property.property_url:
            url = api.property_url(property.property_url)
            webbrowser.open_new_tab(url)

    def _show_route(
        self, property: models.Property, start_coordinate: tuple[float, float]
    ) -> None:
        destination = (
            property.location.latitude,
            property.location.longitude,
        )
        map_url = flathunt.map.url(start_coordinate, destination)
        webbrowser.open_new_tab(map_url)

    def _skip(self) -> bool:
        ans = input('Press "s" to skip, anything else is continue: ')
        return ans == "s"

    def _wait(self, message: str) -> None:
        input(" ".join((message, "Press enter to continue...")))
