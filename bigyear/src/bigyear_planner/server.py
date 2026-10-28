from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from .util import ROOT, read, sha256
from .display import attach_count_bound, attach_scoring_tiers, display_route
from .coverage import attach_observer_coverage


def is_route_file(path):
    return path.name == "route.json" or re.fullmatch(r"alternative-[1-9]\d*\.json", path.name) is not None


def route_entries(root):
    """Discover saved routes without mistaking audit reports or unfinished files for routes."""
    root = Path(root).resolve()
    entries = []
    for main in sorted(root.rglob("route.json")):
        for saved in [main, *sorted(main.parent.glob("alternative-*.json"))]:
            if not is_route_file(saved) or not saved.resolve().is_relative_to(root):
                continue
            try:
                route = read(saved)
                provenance = route.get('provenance') if isinstance(route, dict) else None
                if isinstance(provenance, dict) and (provenance.get('compact_model_sha256') or route.get('marine_extension')):
                    audit = saved.parent / 'validation.json'
                    checked = read(audit) if audit.exists() else None
                    if not isinstance(checked, dict) or checked.get('valid') is not True:
                        continue
                    if route.get('marine_extension') and checked.get('route_sha256')!=sha256(saved):
                        continue
            except (OSError, ValueError):
                continue
            if (not isinstance(route, dict)
                    or type(route.get("year")) is not int
                    or type(route.get("species_total")) is not int
                    or type(route.get("synthetic")) is not bool
                    or not isinstance(route.get("days"), list)):
                continue
            label = str(saved.parent.relative_to(root)) or "."
            if saved != main:
                label += " / " + saved.stem
            entries.append({"path": str(saved.relative_to(root)), "year": route["year"],
                            "species": route["species_total"], "synthetic": route["synthetic"],
                            "label": label})
    return entries


def serve(runs, port=8765):
    root = Path(runs).resolve()
    web = ROOT / "web"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path == "/report":
                target = root / "pilot-report.html"
                if not target.is_file():
                    return self.send_error(404)
                return self.respond(target.read_bytes(), "text/html")
            exports = {
                "itinerary.csv",
                "species-audit.csv",
                "detour-review.csv",
                "run-summary.csv",
                "unreached-species.csv",
                "selected-access-review.csv",
                "count-bound.json",
                "validation.json",
                "run-status.json",
                "comparison.csv",
                "marine-only-candidates.csv",
                "source-decisions.csv",
                "replay-status.json",
                "evaluation-result.json",
                "countability-source-validation.json",
                "marine-cohort.json",
                "marine-access-audit.json",
                "marine-added-species.csv",
                "marine-source-decisions.csv",
                "marine-source-holds.csv",
                "marine-plan.json",
                "marine-access-score-holds.json",
                "result.json",
                "gap-analysis.json",
                "uncollected-species.csv",
                "species-changes.csv",
                "calendar-conflicts.json",
                "calendar-conflict-groups.csv",
                "targeted-species-results.csv",
                "incumbent-species-changes.csv",
                "targeted-exploration.json",
            }
            if Path(path).name in exports:
                target = (root / path.lstrip("/")).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    return self.send_error(404)
                return self.respond(
                    target.read_bytes(),
                    "text/csv" if target.suffix == ".csv" else "application/json",
                )
            if path == "/api/context":
                target = root / "map-context.geojson"
                data = (
                    target.read_bytes()
                    if target.exists()
                    else b'{"type":"FeatureCollection","features":[]}'
                )
                return self.respond(data, "application/json")
            if path == "/api/runs":
                return self.respond(json.dumps(route_entries(root)).encode(), "application/json")
            if path.startswith("/api/route/"):
                target = (root / path[len("/api/route/") :]).resolve()
                if (
                    not target.is_relative_to(root)
                    or not target.is_file()
                    or not is_route_file(target)
                ):
                    return self.send_error(404)
                route = attach_count_bound(root, display_route(root, target))
                route = attach_scoring_tiers(target,route)
                return self.respond(json.dumps(attach_observer_coverage(root, route)).encode(), "application/json")
            target = (web / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
            if not target.is_relative_to(web) or not target.is_file():
                return self.send_error(404)
            content_type = {
                ".html": "text/html",
                ".js": "text/javascript",
                ".css": "text/css",
                ".geojson": "application/json",
                ".json": "application/json",
            }.get(target.suffix, "application/octet-stream")
            self.respond(target.read_bytes(), content_type)

        def respond(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Big Year map: http://127.0.0.1:{port} (saved results under {root})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
