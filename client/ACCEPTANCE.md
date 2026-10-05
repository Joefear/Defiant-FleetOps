# Slice 14 operator walkthrough

Run these steps with a disposable PostgreSQL 16 database and the current capture API.
Use opaque UUID labels for FleetOps entities. A readable manufacturer serial is scanned
as an observation during receiving; it is never typed into an identity lookup.

## Run and verify

Use Node.js 22 or newer and the checked-in lockfile. Start the backend using its
existing runtime configuration. From the verified repository root:

```powershell
npm --prefix client ci --ignore-scripts --cache .local/npm-cache
$env:FLEETOPS_API_URL = 'http://127.0.0.1:8000'
$env:FLEETOPS_PUBLIC_ORIGIN = 'http://127.0.0.1:3000'
npm --prefix client run dev
```

Verify the actual Git root and branch before each install, build, test or other
write-capable phase. The API destination and public origin are administrator settings,
not browser inputs. For a production host, use HTTPS and set its exact public origin.
Localhost is suitable for this walkthrough. No deployment is part of Slice 14.

```powershell
npm --prefix client run check
npm --prefix client run build
.venv\Scripts\python.exe -X utf8 -m pytest server/tests/slice14 -q
```

The browser test starts and stops its own FastAPI and production Next.js processes
and uses only the existing disposable database harness. It locates Node on PATH,
uses installed Windows Edge/Chrome if present, otherwise Playwright's installed
Chromium. FLEETOPS_TEST_BROWSER can name another installed Chromium executable.
Each run uses a fresh headless browser context. No operator profile is reused.

## Receiving the Slice 9 ugly delivery

1. Sign in. Wait for **Offline shell ready**. Select **Receive** and scan the issued
   PO label with a wedge or camera. The label contains just its UUID.
2. The PO expects six workstations, two scanners, one printer and one network unit.
   Scan the receiving dock's Location label and choose **Open receipt**. Opening
   acknowledges the displayed PO comparator generations. It requires connectivity.
3. Explicitly select the owner of the arriving serialized units. The vendor and
   authenticated operator are not substitutes for the owner.
4. Scan the catalog Item label for each physical observation. Receive five of the
   expected workstations. Scan four readable manufacturer serial barcodes. For the
   fifth, choose **Serial is unreadable** and enter **Label abraded** as the reason.
   Never invent a serial or type one in place of the missing barcode.
5. Select **GOOD** as the observed condition. Photograph the unreadable unit using
   **Receipt photo**. Save the observation. The photo stays in IndexedDB until its
   immutable upload and receiving-evidence link both succeed.
6. Scan the substituted workstation's Item label and manufacturer serial. Choose
   the workstation's expected PO line explicitly. Save it as GOOD.
7. Scan and receive one scanner, the printer and the network unit against their
   corresponding PO lines. Capture their actual readable serials by scanning.
8. Scan the unexpected nonserialized accessory's Item label. Choose **Unexpected
   Item · no PO line**, observe quantity one and condition GOOD, then save.
9. Sync all units/photos and select **Finish receipt**. Finishing needs connectivity
   and refuses locally pending or rejected observations. Confirm ten receipt lines,
   nine serialized Assets and exactly SUBSTITUTION, SERIAL_UNREADABLE, SHORT and
   UNEXPECTED_ITEM. The accessory creates no Asset. No transient shortage is derived
   while the receipt remains open.
10. Reopen an open receipt by scanning its Receipt label. Its acknowledged comparator
    sources remain pinned; receipt-bound PO corrections remain forbidden. A scan of a different Item
    does not silently change the expected line.

## Move, Assign and Transition

- **Move:** scan the Asset, scan the destination Location, record a reason and save.
- **Assign:** scan the Asset, then scan a station Location or select an active human
  Actor from server-provided choices. Record a reason and save.
- **Transition:** scan the Asset and choose from the server-provided legal next
  states. The client has no lifecycle graph. ON_HOLD exits come from effective
  entry history. Retirement requires a scanned attachment already linked as
  disposal evidence. Record a reason and save.

Each capture retains the observed global Asset version. Another queued operation
does not predict or advance that version. Scan again for a new observation.

## Offline replay and conflict inbox

1. While connected, scan three different Assets and their destination to load the
   facts needed offline. Keep the verified, unexpired operator session in this tab.
2. Make the API unreachable, disconnect the browser and reload the app. Confirm the
   static shell still renders. Save one move for each of the three Assets.
3. Confirm three pending captures survive with their original IDs, client ID,
   epoch, sequence, payload, occurrence time and expected versions.
4. Before restoring API access, have a second operation move the middle Asset and
   advance its global version. Restore connectivity and choose **Sync now**.
5. Confirm capture-order replay: APPLIED, REJECTED, APPLIED. The rejected operation
   opens a SYNC_CONFLICT. No new operation ID, version or implicit retry is created.
6. Open **Inbox**. Compare **Captured expectation** and **Server facts**, including
   their versions/location/state. A human may record a resolution note. Resolving
   acknowledges the exception; it does not apply the rejected movement.
7. Inspect CacheStorage: only static shell assets are present, never API responses
   or credentials. Changing Actor/tenant cannot replay another Actor's buffer.

## Executed automation and limits

The live-browser test executes the receiving scenario, immutable photo linking,
assignment, transition, full offline reload, three queued captures, ordered replay
and visible conflict against real PostgreSQL 16. It supplies real Code128 and Data
Matrix images through Chromium's virtual camera to the production video decoder.
Keyboard-wedge inputs use the scan control and Enter. The test independently queries
stored receiving, capture, movement and evidence facts.

The live-browser suite includes a separate tab where the browser denies its session
storage property. Startup remains signed out; attempted sign-in reports storage
refusal without page errors or a false signed-in result. The main offline replay
tab retains normal storage behavior. Session unit tests cover denied reads/removal,
invalid stored credentials, round trip and denied/quota-refused login persistence.

The queue unit suite exercises concurrent IDB allocation, capture order across epoch reset,
actor/tenant separation, response loss after admission, original duplicate rejection,
unavailable outcomes, current-credential mismatch, cache-denial behavior, crashed
lease ownership and photo-link replay. Proxy regressions cover public origin,
cross-origin rejection, fixed destination, cookie exclusion, transport loss and
upload bounds. The receipt-read proof verifies original and pre-binding corrected generations,
and confirms that receipt-bound PO corrections are refused. Full regression and independent review remain separate release gates.

Physical camera focus/lighting, wedge hardware, printer output, mobile OS background
wake scheduling and device storage eviction are not measured by the virtual-device
tests. Background Sync is progressive enhancement; online, tab visibility, periodic
and manual sync wake the same worker. A restarted worker waits for a page-supplied
current credential. Without that credential it performs no authorized replay.
Do not clear browser storage while captures/photos are pending; this local buffer
is not a backup. An uncertain photo-upload response may leave an unused immutable
upload; the persisted link operation still replays with one stable identity.
