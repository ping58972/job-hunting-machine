"""Root-local fake ATS pages served only through Playwright route fulfillment."""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from playwright.async_api import Route

FAKE_ATS_ORIGIN = "https://fake-ats.invalid"


@dataclass(slots=True)
class FakeATSApplication:
    ats: str = "GREENHOUSE"
    extra_field: bool = False
    captcha: bool = False
    mfa: bool = False
    single_page_submit: bool = False
    transcript: bool = False
    requests: list[tuple[str, str]] = field(default_factory=list)

    def html(self, path: str) -> str:
        challenge = "<p>CAPTCHA verification</p>" if self.captcha else ""
        challenge += "<p>Multi-factor verification code</p>" if self.mfa else ""
        if path == "/review":
            return f"""<!doctype html><html><body data-ats="{self.ats}" data-page-key="review">
            <h1>Review application</h1><button type="button"
            onclick="fetch('/submitted',{{method:'POST'}}).then(()=>location.href='/confirmation')">
            Submit Application</button></body></html>"""
        if path == "/confirmation":
            return "<!doctype html><html><body><h1>Application submitted</h1></body></html>"
        unknown = (
            '<label for="portfolio">Favorite robot</label><input id="portfolio" '
            'data-jhm-field="favorite_robot" required>'
            if self.extra_field
            else ""
        )
        button = (
            "<button type='button' onclick=\"fetch('/submitted',{method:'POST'})\">"
            "Submit Application</button>"
            if self.single_page_submit
            else "<button data-jhm-next type='button' "
            "onclick=\"location.href='/review'\">Continue</button>"
        )
        transcript = (
            '<label for="transcript">Transcript</label><input id="transcript" '
            'name="transcript" data-jhm-field="transcript" type="file" required>'
            if self.transcript
            else ""
        )
        return f"""<!doctype html><html><body data-ats="{self.ats}" data-page-key="contact">
        {challenge}<h1>Application</h1>
        <label for="first">First name</label>
        <input id="first" name="first_name" data-jhm-field="first_name"
        autocomplete="given-name" required>
        <label for="last">Last name</label>
        <input id="last" name="last_name" data-jhm-field="last_name"
        autocomplete="family-name" required>
        <label for="resume">Resume</label>
        <input id="resume" name="resume" data-jhm-field="resume" type="file" required>
        {transcript}{unknown}{button}
        </body></html>"""

    async def fulfill(self, route: Route) -> None:
        request = route.request
        parsed = urlsplit(request.url)
        self.requests.append((request.method, parsed.path))
        if request.method != "GET":
            await route.fulfill(status=204, body="")
            return
        await route.fulfill(status=200, body=self.html(parsed.path), content_type="text/html")
