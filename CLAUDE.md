# Atechs oppstartsprompt

Ordrett fra https://atech.dev/docs, under «Build with your own agent».

Install the Atech Python SDK and use it to build my project:

    pip install atech

It needs Python 3.10 or newer. Then run `atech guide` and read all of it before
anything else: it is the SDK's own manual for coding agents (the workflow, the
rules, what will bite you). `atech guide protocol` covers the wire format.

Atech is a modular electronics platform: pre-built hardware modules snap into
numbered ports on an ESP32 motherboard, with no soldering or wiring. The SDK
ships the module catalog, each module's API, and a bundled compiler that builds
and flashes the board offline.

Before you write any code, ask me:

1. Which Atech board do I have? Run `atech list-boards` to see the boards and
   their ports.
2. Which modules do I own? Run `atech list-modules` to see every module the
   SDK knows about, and ask me which of those I actually have in hand.
3. Which port is each module plugged into?

If I don't have a board yet, or I'm missing a module the build needs, point me
to https://atech.dev/buy and suggest which kit fits what I described.

Then:

- Run `atech new <name> --board <board id>` and describe the build in its
  project.yaml, using only modules I confirmed I own and the ports I confirmed
  they're in. `atech info <module>` shows a module's API.
- Run `atech validate`, then `atech build`, and fix anything they report.
  Tell me first that the first build downloads the ESP32 compiler, about
  2.4 GB, once.
- Run `atech upload` with my board on USB-C (a data cable), then `atech check`
  to see which modules answered, and tell me what to expect it to do.
