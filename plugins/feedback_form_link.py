"""
Airflow plugin: add a "Feedback Form" entry to the nav / sidebar.

Same trick as the pgAdmin link: a nav item with ``destination="nav"``, an
absolute ``href``, and no ``url_route`` renders as a plain new-tab link
(``<a href target="_blank">``) instead of an in-app iframe. Clicking it opens
the Google Form directly in a new tab.
"""

from __future__ import annotations

import base64

from airflow.plugins_manager import AirflowPlugin

FEEDBACK_FORM_URL = "https://forms.gle/WHDzsXr3umUsAGTs8"

# Inline clipboard glyph as a data URI so the nav item has an icon without
# pulling in any external asset.
_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
    "stroke='#673ab7' stroke-width='2' stroke-linecap='round' "
    "stroke-linejoin='round'>"
    "<path d='M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2'/>"
    "<rect x='8' y='2' width='8' height='4' rx='1' ry='1'/>"
    "<path d='M9 12h6'/><path d='M9 16h4'/>"
    "</svg>"
)
_FEEDBACK_ICON = "data:image/svg+xml;base64," + base64.b64encode(_SVG.encode()).decode()


class FeedbackFormLinkPlugin(AirflowPlugin):
    name = "feedback_form_link"
    external_views = [
        {
            "name": "Feedback Form",
            "href": FEEDBACK_FORM_URL,
            # No url_route, so the UI renders this as a plain new-tab link
            # straight to the form instead of an in-app iframe.
            "destination": "nav",
            "icon": _FEEDBACK_ICON,
        }
    ]
