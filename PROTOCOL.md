# Protocol

Both apps are symmetric peers on the same Wi-Fi/LAN.

**Discovery** - UDP broadcast to port `53318` every 3 s, JSON:
`{"app":"wad","id":"<12 hex>","name":"<device>","port":53317,"os":"windows|android","pin":true|false}`
A peer is dropped after 12 s of silence.

**Transfer** - `PUT http://<ip>:53317/send?name=<url-encoded filename>`
Headers: `Content-Length` (required), `X-Device` (url-encoded sender name), `X-Pin` (if receiver has a PIN).
Responses: `200 ok`, `403 bad pin`, `411`, `404`.
Receivers sanitize the filename and never overwrite existing files.
