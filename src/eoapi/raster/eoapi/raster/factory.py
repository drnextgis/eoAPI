"""Custom MultiBaseTilerFactory."""

import os
from dataclasses import dataclass
from typing import Any, List, Optional

from psycopg import sql
from psycopg.rows import class_row
from pydantic import BaseModel

from fastapi import Depends, Query
from starlette.datastructures import QueryParams
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates
from titiler.core import factory as TitilerFactory
from titiler.pgstac import factory as TitilerPgSTACFactory
from titiler.pgstac import model

try:
    from importlib.resources import files as resources_files  # type: ignore
except ImportError:
    # Try backported to PY<39 `importlib_resources`.
    from importlib_resources import files as resources_files  # type: ignore


# TODO: mypy fails in python 3.9, we need to find a proper way to do this
templates = Jinja2Templates(directory=str(resources_files(__package__) / "templates"))  # type: ignore


@dataclass
class MultiBaseTilerFactory(TitilerFactory.MultiBaseTilerFactory):
    """Custom endpoints factory."""

    def register_routes(self) -> None:
        """This Method register routes to the router."""
        super().register_routes()

        # Add viewer
        @self.router.get("/viewer", response_class=HTMLResponse)
        def stac_demo(
            request: Request,
            src_path: Any = Depends(self.path_dependency),
        ):
            """STAC Viewer."""
            return templates.TemplateResponse(
                name="stac-viewer.html",
                context={
                    "request": request,
                    "endpoint": request.url.path.replace("/viewer", ""),
                    "stac_url": request.query_params[
                        "url"
                    ],  # Warning: This assume that `self.path_dependency` uses `url=`
                },
                media_type="text/html",
            )


class Infos(BaseModel):
    """Response model for /list endpoint."""

    searches: List[model.Info]
    links: Optional[List[model.Link]]
    numberMatched: Optional[int]
    numberReturned: Optional[int]


@dataclass
class MosaicTilerFactory(TitilerPgSTACFactory.MosaicTilerFactory):
    """Custom endpoints factory."""

    enable_mosaic_search: bool = False

    def register_routes(self) -> None:
        """This Method register routes to the router."""
        super().register_routes()
        if self.enable_mosaic_search:
            self._mosaic_search()

    def _mosaic_search(self) -> None:
        """register mosaic search route."""

        @self.router.get(
            "/list",
            responses={200: {"description": "List Mosaics in PgSTAC."}},
            response_model=Infos,
            response_model_exclude_none=True,
        )
        def list_mosaic(
            request: Request,
            limit: int = Query(
                10,
                ge=1,
                le=int(os.environ.get("EOAPI_RASTER_MAX_MOSAIC", "10000")),
                description="Page size limit",
            ),
            offset: int = Query(
                0,
                ge=0,
                description="Page offset",
            ),
        ):
            """List a Search query."""
            offset_and_limit = [
                sql.SQL("LIMIT {number}").format(number=sql.Literal(limit)),
                sql.SQL("OFFSET {start}").format(start=sql.Literal(offset)),
            ]

            # filter to only return `metadata->type == 'mosaic'`
            mosaic_filter = sql.SQL("metadata::json->>{key} = {value}").format(
                key=sql.Literal("type"), value=sql.Literal("mosaic")
            )

            # additional metadata property filter
            # <propname>=val - filter for a metadata property. Multiple property filters are ANDed together.
            qs_key_to_remove = ["limit", "offset", "properties", "sortby"]
            additional_filter = [
                sql.SQL("metadata::json->>{key} = {value}").format(
                    key=sql.Literal(key), value=sql.Literal(value)
                )
                for (key, value) in request.query_params.items()
                if key.lower() not in qs_key_to_remove
            ]
            filters = [
                sql.SQL("WHERE"),
                sql.SQL("AND ").join([mosaic_filter, *additional_filter]),
            ]

            # TODO: enable SortBy
            with request.app.state.dbpool.connection() as conn:
                with conn.cursor() as cursor:
                    # Get Total Number of searches rows
                    query = [
                        sql.SQL("SELECT count(*) FROM searches"),
                        *filters,
                    ]
                    cursor.execute(sql.SQL(" ").join(query))
                    nb_items = cursor.fetchone()[0]

                    # Get rows
                    cursor.row_factory = class_row(model.Search)
                    query = [
                        sql.SQL("SELECT * FROM searches"),
                        *filters,
                        *offset_and_limit,
                    ]

                    cursor.execute(sql.SQL(" ").join(query))

                    searches_info = cursor.fetchall()

            qs = QueryParams({**request.query_params, "limit": limit, "offset": offset})
            links = [
                model.Link(
                    rel="self",
                    href=self.url_for(request, "list_mosaic") + f"?{qs}",
                ),
            ]

            if len(searches_info) < int(nb_items):
                next_token = offset + len(searches_info)
                qs = QueryParams(
                    {**request.query_params, "limit": limit, "offset": next_token}
                )
                links.append(
                    model.Link(
                        rel="next",
                        href=self.url_for(request, "list_mosaic") + f"?{qs}",
                    ),
                )

            if offset > 0:
                prev_token = offset - limit if (offset - limit) > 0 else 0
                qs = QueryParams(
                    {**request.query_params, "limit": limit, "offset": prev_token}
                )
                links.append(
                    model.Link(
                        rel="prev",
                        href=self.url_for(request, "list_mosaic") + f"?{qs}",
                    ),
                )

            return Infos(
                searches=[
                    model.Info(
                        search=search,
                        links=[
                            model.Link(
                                rel="metadata",
                                href=self.url_for(
                                    request, "info_search", searchid=search.id
                                ),
                            ),
                            model.Link(
                                rel="tilejson",
                                href=self.url_for(
                                    request, "tilejson", searchid=search.id
                                ),
                            ),
                        ],
                    )
                    for search in searches_info
                ],
                links=links,
                numberMatched=int(nb_items),
                numberReturned=len(searches_info),
            )
