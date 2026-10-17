Captured from the real CLI (`gh api -i`), then scrubbed: URL-bearing and account headers dropped, bodies replaced by a fixture issue.
Observed on 17 Oct 2026:
- 200 with an ETag: exit status 0, headers then body on stdout, the ETag is weak (W/ prefix).
- 304 on a repeat with If-None-Match: exit status 1, stdout holds the status line and headers with no body, stderr holds `gh: HTTP 304`.
- 404: exit status 1, stdout holds status line, headers and a JSON message body, stderr holds `gh: Not Found (HTTP 404)`.
The ETag sent back is the stored one including its W/ prefix, and the host answered 304.
