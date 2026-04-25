import asyncio
import json
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

CLAUDE_BIN = "/opt/claude.exe"
HOME_DIR = "/home/claude"


class PromptRequest(BaseModel):
    system: str = ""
    prompt: str
    oauth_token: str = ""   # per-request override for user subscriptions


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/analyze")
async def analyze(req: PromptRequest):
    full_prompt = req.prompt

    env = {
        "HOME": HOME_DIR,
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "NODE_NO_WARNINGS": "1",
        "TERM": "xterm",
    }
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_API_KEY"):
        val = os.environ.get(key)
        if val:
            env[key] = val

    # Per-request OAuth token takes priority over server token
    if req.oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = req.oauth_token
    else:
        val = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if val:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = val

    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN,
            "--dangerously-skip-permissions",
            "-p", full_prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Claude CLI timed out")

    raw = stdout.decode().strip()
    if not raw:
        err = stderr.decode().strip()
        raise HTTPException(status_code=500, detail=f"Claude CLI returned no output. stderr: {err[:300]}")

    # Strip markdown fences if present
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

    # Try JSON parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start:end])
            except Exception:
                pass
        # Return raw text wrapped
        return {"raw_text": raw}
