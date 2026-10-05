"""Real PostgreSQL, live FastAPI and production Next.js execute the scan walkthrough.

Only this test's sockets, child process, temp evidence and disposable database are
owned. Control endpoints bind localhost on a separate test-only HTTP server and are
never added to the application. Credentials reach Node through stdin, not arguments.
"""

import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections import Counter
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen
from uuid import UUID

import pytest
import uvicorn
import zxingcpp
from fastapi.testclient import TestClient
from PIL import Image
from server.tests.slice5.conftest import headers
from sqlalchemy import select

from fleetops.api.app import create_app
from fleetops.auth import hash_password
from fleetops.db.metadata import (
    asset_movements,
    assets,
    attachment_links,
    capture_operations,
    users,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("barcode_format", ["CODE128", "DATAMATRIX"])
def test_real_browser_receiving_and_offline_replay(
    barcode_format,
    database,
    space_data,
    seed_asset,
    make_item,
    make_order,
    migrator_connection,
    tmp_path,
):
    """Execute Slice 9 ugly receiving, photo link and three ordered offline captures."""
    tenant = space_data[0]
    password = secrets.token_urlsafe(24)
    username = "browser-" + str(tenant.user_id)
    migrator_connection.execute(
        users.update()
        .where(users.c.id == tenant.user_id)
        .values(
            username=username,
            password_hash=hash_password(password),
        )
    )
    migrator_connection.commit()
    substitute, scanner, printer, network = [make_item(serialized=True) for _ in range(4)]
    accessory = make_item()
    order, lines = make_order(
        specs=[
            {"item_id": tenant.item_id, "quantity": 6},
            {"item_id": scanner, "quantity": 2},
            {"item_id": printer, "quantity": 1},
            {"item_id": network, "quantity": 1},
        ]
    )
    units = [
        {
            "item": str(tenant.item_id),
            "comparator": str(lines[0]["id"]),
            **({"unreadable": True} if index == 0 else {"serial": "WORK-" + str(index)}),
        }
        for index in range(5)
    ]
    units.append({"item": str(substitute), "comparator": str(lines[0]["id"]), "serial": "SUB-1"})
    for index, item in enumerate((scanner, printer, network), 1):
        units.append(
            {
                "item": str(item),
                "comparator": str(lines[index]["id"]),
                "serial": "OTHER-" + str(index),
            }
        )
    units.append({"item": str(accessory)})
    fixtures = [seed_asset(tenant)["id"] for _ in range(3)]
    # A real barcode enters Chromium's virtual camera, then the production ZXing
    # video reader. This proves both symbologies without needing physical hardware.
    symbol = Image.fromarray(
        zxingcpp.create_barcode(
            str(order["id"]),
            zxingcpp.BarcodeFormat.Code128
            if barcode_format == "CODE128"
            else zxingcpp.BarcodeFormat.DataMatrix,
        ).to_image(
            scale=2 if barcode_format == "CODE128" else 8, add_hrt=False, add_quiet_zones=True
        )
    ).convert("L")
    frame = Image.new("L", (1280, 720), 255)
    frame.paste(symbol, ((1280 - symbol.width) // 2, (720 - symbol.height) // 2))
    camera = tmp_path / "camera.y4m"
    with camera.open("wb") as stream:
        stream.write(b"YUV4MPEG2 W1280 H720 F5:1 Ip A0:0 C420jpeg\n")
        pixels = frame.tobytes() + bytes([128]) * (1280 * 720 // 2)
        for _ in range(10):
            stream.write(b"FRAME\n" + pixels)
    app = create_app(replace(database.settings(tenant.org_id), evidence_root=tmp_path / "evidence"))
    api_server = None
    api_thread = None
    api_port = 0

    def start_api():
        nonlocal api_server, api_thread, api_port
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", api_port))
        api_port = listener.getsockname()[1]
        listener.listen()
        api_server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        api_thread = threading.Thread(
            target=api_server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        api_thread.start()
        deadline = time.monotonic() + 15
        while not api_server.started:
            assert time.monotonic() < deadline, "Owned API did not start"
            time.sleep(0.05)

    def stop_api():
        if api_server:
            api_server.should_exit = True
            api_thread.join(15)
            assert not api_thread.is_alive(), "Owned API did not stop"

    start_api()

    class Control(BaseHTTPRequestHandler):
        """Test-only coordination creates a deterministic outage/conflict schedule."""

        def do_POST(self):
            try:
                if self.path == "/stop":
                    stop_api()
                elif self.path == "/start":
                    # The API is still unreachable to the browser until this response.
                    with TestClient(app) as client:
                        current = client.get(
                            f"/assets/{fixtures[1]}", headers=headers(tenant)
                        ).json()
                        result = client.post(
                            f"/assets/{fixtures[1]}/movements",
                            headers=headers(tenant),
                            json={
                                "expected_version": current["version"],
                                "to_location_id": None,
                                "reason": "Independent operator during outage",
                                "occurred_at": "2026-10-04T12:00:00Z",
                            },
                        )
                        assert result.status_code == 201, result.text
                    start_api()
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception:
                self.send_error(500)

        def log_message(self, *args):
            pass

    control = ThreadingHTTPServer(("127.0.0.1", 0), Control)
    control_thread = threading.Thread(target=control.serve_forever, daemon=True)
    control_thread.start()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = os.environ.copy()
    env.update(FLEETOPS_API_URL=f"http://127.0.0.1:{api_port}", NEXT_TELEMETRY_DISABLED="1")
    log = (tmp_path / "next.log").open("w", encoding="utf-8")
    node = shutil.which("node")
    assert node, "Install project-compatible Node.js before browser acceptance"
    browser = os.environ.get("FLEETOPS_TEST_BROWSER") or next(
        (
            str(path)
            for path in (
                Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            )
            if path.is_file()
        ),
        None,
    )
    process = subprocess.Popen(
        [
            str(node),
            str(ROOT / "client/node_modules/next/dist/bin/next"),
            "start",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT / "client",
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            assert process.poll() is None, (tmp_path / "next.log").read_text(encoding="utf-8")
            try:
                with urlopen(f"http://127.0.0.1:{port}", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                assert time.monotonic() < deadline, "Owned Next server did not start"
                time.sleep(0.1)
        result = subprocess.run(
            [str(node), str(ROOT / "client/tests/browser.mjs")],
            input=json.dumps(
                {
                    "browser": browser,
                    "video": str(camera),
                    "url": f"http://127.0.0.1:{port}",
                    "control": f"http://127.0.0.1:{control.server_port}",
                    "username": username,
                    "password": password,
                    "order": str(order["id"]),
                    "location": str(tenant.location_id),
                    "owner": str(tenant.party_id),
                    "actor": str(tenant.actor_id),
                    "assets": [str(value) for value in fixtures],
                    "delivery": units,
                    "screenshot": str(tmp_path / "offline-inbox.png"),
                }
            ),
            cwd=ROOT / "client",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        # Locator snapshots can contain password inputs; even disposable credentials
        # must not enter test diagnostics.
        diagnostic = (result.stdout + result.stderr).replace(password, "[redacted]")
        assert result.returncode == 0, diagnostic
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        with TestClient(app) as client:
            received = client.get(
                f"/receipts/{proof['receipt_id']}", headers=headers(tenant)
            ).json()
        assert received["reconciled"] and len(received["lines"]) == 10
        assert Counter(row["exception_type"] for row in received["exceptions"]) == Counter(
            {
                "SUBSTITUTION": 1,
                "SERIAL_UNREADABLE": 1,
                "SHORT": 1,
                "UNEXPECTED_ITEM": 1,
            }
        )
        received_assets = [UUID(line["asset_id"]) for line in received["lines"] if line["asset_id"]]
        assert len(received_assets) == 9
        stored = (
            migrator_connection.execute(select(assets).where(assets.c.id.in_(received_assets)))
            .mappings()
            .all()
        )
        assert all(
            row["version"] == 1
            and row["current_state"] == "RECEIVED"
            and row["owner_party_id"] == tenant.party_id
            for row in stored
        )
        replay = (
            migrator_connection.execute(
                select(capture_operations)
                .where(
                    capture_operations.c.operation_id.in_(
                        [UUID(value) for value in proof["offline_operations"]]
                    )
                )
                .order_by(capture_operations.c.recorded_at)
            )
            .mappings()
            .all()
        )
        assert [str(row["operation_id"]) for row in replay] == proof["replay_order"]
        assert [row["sync_state"] for row in replay] == ["APPLIED", "REJECTED", "APPLIED"]
        assert migrator_connection.execute(
            select(attachment_links).where(
                attachment_links.c.org_id == tenant.org_id,
                attachment_links.c.link_role == "RECEIVING_EVIDENCE",
            )
        ).mappings().one()["entity_id"] == UUID(received["lines"][0]["id"])
        assert (
            len(
                migrator_connection.execute(
                    select(asset_movements).where(
                        asset_movements.c.asset_id == fixtures[1],
                    )
                ).all()
            )
            == 1
        )
        migrator_connection.rollback()
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(10)
        log.close()
        control.shutdown()
        control.server_close()
        control_thread.join(5)
        stop_api()
