"""Server-local installer: stdin contains private model config, never printed.

Creates new service identities only if absent, preserves all business secrets.
"""
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

def main():
    incoming=json.load(sys.stdin)
    project=Path(os.environ.get("POWERSYSTEM_DIR","/opt/powersystem"))
    config=Path("/etc/powersystem")
    env_file=project/".env"
    values={}
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key,value=line.split("=",1);values[key]=value
    if not values.get("REDIS_PASSWORD"):
        raise RuntimeError("private Redis configuration missing")
    for name in ("agent-service-token","agent-monitor-token"):
        target=config/name
        if not target.exists():
            target.write_text(secrets.token_hex(32))
        os.chown(target,10001,10001);target.chmod(0o440 if name == "agent-monitor-token" else 0o400)
    key=config/"agent-model-key"
    key.write_text(incoming.get("api_key", ""))
    os.chown(key,10001,10001);key.chmod(0o400)
    audit=project/"runtime/agent-audit"
    audit.mkdir(parents=True,exist_ok=True);os.chown(audit,10001,10001);audit.chmod(0o700)
    updates={
        "AGENT_ENABLED":"true","AGENT_INTERPRET_ENABLED":"true",
        "AGENT_LLM_BASE_URL":incoming.get("base_url",""),"AGENT_LLM_MODEL":incoming.get("model",""),
        "AGENT_LLM_API_KEY_FILE":str(key),"AGENT_SERVICE_TOKEN_FILE":str(config/"agent-service-token"),
        "AGENT_MONITOR_TOKEN_FILE":str(config/"agent-monitor-token"),
        "AGENT_REDIS_URL":"redis://:"+quote(values["REDIS_PASSWORD"],safe="")+"@redis:6379/0",
        "AGENT_AUDIT_HOST_DIR":str(audit),
    }
    lines=[line for line in env_file.read_text().splitlines() if line.split("=",1)[0] not in updates]
    lines += [key+"="+value for key,value in updates.items()]
    env_file.write_text("\n".join(lines)+"\n");env_file.chmod(0o600)
    print("Agent private configuration installed; business secrets preserved.")

if __name__=="__main__": main()
