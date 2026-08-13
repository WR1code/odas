# Local UI clarity update

AV-Twin Android Responder v0.9.0 ACK+POSE.

- Separates the Android ARM listening address from the Linux result destination.
- Labels both UDP directions and ports in Chinese.
- Makes the entire activity vertically scrollable in landscape and on short displays.
- Replaces the send-only UDP check with a nonce-matched round trip; PASS means
  Android → Linux → Android both succeeded and includes RTT/source diagnostics.
- Lets the Android control listener answer the same test when Linux initiates it.
- Adds editable X/Y/Z and yaw/pitch/roll fields that remain available during a session.
- Freezes the applied pose at every accepted C1 and stores it in JSONL, CSV, and reply UDP metadata.
- Supports repeated measurements with pose revision and measurement ID association.
- Does not change the acoustic detector, C2 playback, STRICT ARM, or acoustic timing protocol.
