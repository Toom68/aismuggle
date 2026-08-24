# QuickSearch (aisearch)

A small tool that disguises OpenAI chat traffic as ordinary web-search requests
and streaming search-result pages. The server looks like a generic search site;
the real AI traffic is smuggled inside as AES-GCM-encrypted `q` parameters and
search-result `snippet` fields.

## Layout

| File            | Role                                                  |
|-----------------|-------------------------------------------------------|
| `crypto.py`     | Shared AES-GCM encrypt/decrypt helpers               |
| `server.py`     | FastAPI app: `GET /` (homepage), `POST /search`     |
| `client.py`     | CLI: `keygen`, `ask`, `decode`                       |
| `requirements.txt` | Python deps                                       |
| `Dockerfile`    | Container image for the server                       |
| `render.yaml`   | Render blueprint                                      |

## How the disguise works

- **Request:** the client POSTs to `/search` with a form field `q` containing
  `base64( nonce || AES-GCM( {"messages":[...],"model":"..."} ) )`. To an
  observer it looks like a search form submission.
- **Response:** the server streams newline-delimited JSON "search result pages":
  ```json
  {"results":[{"title":"Featured snippet","url":"https://...","snippet":"<enc>"}],"page":1,"done":false}
  {"results":[],"page":N,"done":true}
  ```
  Each `snippet` is `base64( nonce || AES-GCM( token ) )`. The `title`/`url`
  fields are decoys. The client decrypts the snippets and prints the completion.

## Deploy on Render

1. Push this folder to a git repo and connect it to Render (or use the
   blueprint: `render.yaml`).
2. Generate a shared key locally:
   ```bash
   python client.py keygen
   ```
3. In the Render service's Environment tab, set:
   - `AISEARCH_KEY`  -> the key from step 2
   - `OPENAI_API_KEY` -> your OpenAI key
   - `OPENAI_MODEL`  -> `gpt-4o-mini` (optional)
4. Deploy. Note the service URL, e.g. `https://quicksearch-xxxx.onrender.com`.

## Use the client

```bash
export AISEARCH_KEY=<same key as the server>
export AISEARCH_URL=https://quicksearch-xxxx.onrender.com

# basic
python client.py ask "Explain entropy in one paragraph"

# with a system prompt / custom model
python client.py ask --system "You are a terse assistant" --model gpt-4o "What is 2+2?"

# pipe a prompt in
echo "summarize quantum tunneling" | python client.py ask

# save the disguised stream, then decode later
python client.py ask --raw "hello" > search.log
python client.py decode < search.log
```

## Run the server locally

```bash
pip install -r requirements.txt
export AISEARCH_KEY=$(python client.py keygen)
export OPENAI_API_KEY=sk-...
uvicorn server:app --port 8000

# in another shell, pointing the client at the local server:
export AISEARCH_URL=http://localhost:8000
python client.py ask "hello"
```

## Notes

- The disguise is on the application layer (paths, form fields, JSON shape).
  The TLS connection still terminates at your Render hostname; an observer with
  SNI visibility can see that you are talking to your Render service (which
  itself looks like a search site). It does **not** see `api.openai.com`.
- AES-GCM gives confidentiality + integrity: the server cannot be tricked into
  serving plaintext, and a passive observer only sees base64 blobs inside
  search-shaped JSON.
- `crypto.py` uses a 12-byte random nonce per message; keys are 32-byte
  urlsafe-base64 strings.
