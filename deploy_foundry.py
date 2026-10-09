"""Deploy the scanner as a Foundry hosted agent (code deployment, remote build).

Follows the Python path of
https://learn.microsoft.com/azure/foundry/agents/quickstarts/quickstart-hosted-agent
but keeps the new version and routes 100 % of traffic to it.

    az login                                   # account with "Foundry Project Manager" on the project
    uv run python deploy_foundry.py            # deploy
    uv run python deploy_foundry.py --invoke "Scan starten"   # starts in background mode and polls

Only the files in INCLUDE are uploaded – never .env.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentEndpointConfig,
    CodeConfiguration,
    CodeDependencyResolution,
    FixedRatioVersionSelectionRule,
    HostedAgentDefinition,
    ProtocolConfiguration,
    ProtocolVersionRecord,
    ResponsesProtocolConfiguration,
    VersionSelector,
)
from azure.core.exceptions import ServiceRequestError
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).parent
INCLUDE = ["main.py", "langgraph.json", "requirements.txt", "scanner"]
AGENT_NAME = os.getenv("FOUNDRY_HOSTED_AGENT_NAME", "akquise-scanner")


def project_endpoint() -> str:
    ep = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_OPENAI_ENDPOINT", "")
    if "/api/projects/" not in ep:
        sys.exit("FOUNDRY_PROJECT_ENDPOINT fehlt (https://<resource>.services.ai.azure.com/api/projects/<project>).")
    return ep.rstrip("/")


def build_zip() -> Path:
    path = Path(tempfile.gettempdir()) / f"{AGENT_NAME}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in INCLUDE:
            src = ROOT / name
            files = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
            for f in files:
                z.write(f, f.relative_to(ROOT).as_posix())
    return path


def deploy(client: AIProjectClient, endpoint: str) -> None:
    model = os.environ["AZURE_OPENAI_DEPLOYMENT"]
    with build_zip().open("rb") as code:
        created = client.agents.create_version_from_code(
            agent_name=AGENT_NAME,
            description="Medien-Scanner für Akquiseimpulse: LangGraph multi-agent scan of regional public sources.",
            definition=HostedAgentDefinition(
                cpu="1",
                memory="2Gi",
                code_configuration=CodeConfiguration(
                    runtime="python_3_13",
                    entry_point=["python", "-m", "langchain_azure_ai.agents.hosting.run", "--protocol", "responses"],
                    dependency_resolution=CodeDependencyResolution.REMOTE_BUILD,
                ),
                environment_variables={
                    "FOUNDRY_PROJECT_ENDPOINT": endpoint,
                    "AZURE_AI_MODEL_DEPLOYMENT_NAME": model,
                    "DATA_DIR": "/tmp/scanner-data",  # container disk is not persistent
                },
                protocol_versions=[ProtocolVersionRecord(protocol="responses", version="2.0.0")],
            ),
            code=code,
        )
    print(f"Version {created.version} erstellt, warte auf Bereitstellung …")
    for _ in range(90):
        time.sleep(10)
        try:
            details = client.agents.get_version(agent_name=AGENT_NAME, agent_version=created.version)
        except ServiceRequestError:  # network blip while waiting
            continue
        print("  Status:", details["status"])
        if details["status"] == "active":
            break
        if details["status"] == "failed":
            sys.exit(f"Bereitstellung fehlgeschlagen: {dict(details)}")
    else:
        sys.exit("Zeitüberschreitung beim Warten auf die Bereitstellung.")
    client.agents.update_details(
        agent_name=AGENT_NAME,
        agent_endpoint=AgentEndpointConfig(
            version_selector=VersionSelector(
                version_selection_rules=[FixedRatioVersionSelectionRule(agent_version=created.version, traffic_percentage=100)]
            ),
            protocol_configuration=ProtocolConfiguration(responses=ResponsesProtocolConfiguration()),
        ),
    )
    print(f"Agent «{AGENT_NAME}» Version {created.version} ist aktiv: {endpoint}/agents/{AGENT_NAME}")


def invoke(client: AIProjectClient, text: str | None, response_id: str | None = None) -> None:
    """A scan runs for minutes, but Foundry cuts a waiting request after about 100 s
    (proxy_timeout). So the scan is started in background mode and polled."""
    openai_client = client.get_openai_client(agent_name=AGENT_NAME).with_options(max_retries=0)
    if response_id is None:
        response_id = openai_client.responses.create(input=text, background=True, timeout=120).id
        print(f"Gestartet: {response_id} (fortsetzen mit --resume {response_id})", file=sys.stderr)
    start = time.time()
    while time.time() - start < 1800:
        try:
            response = openai_client.responses.retrieve(response_id, timeout=60)
        except Exception as e:  # a network blip while polling must not lose the run
            print(f"  Abfrage fehlgeschlagen ({type(e).__name__}), neuer Versuch …", file=sys.stderr)
            time.sleep(15)
            continue
        if response.status not in ("queued", "in_progress"):
            print(response.output_text or f"Status {response.status}: {response.error}")
            return
        print(f"  {int(time.time() - start)} s: {response.status}", file=sys.stderr)
        time.sleep(20)
    sys.exit(f"Nach 30 Minuten noch nicht fertig. Später: --resume {response_id}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--invoke", metavar="TEXT", help="nur den bereitgestellten Agenten aufrufen")
    p.add_argument("--resume", metavar="ID", help="Ergebnis eines gestarteten Laufs abholen")
    a = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    endpoint = project_endpoint()
    with DefaultAzureCredential() as cred, AIProjectClient(endpoint=endpoint, credential=cred) as client:
        if a.invoke or a.resume:
            invoke(client, a.invoke, a.resume)
        else:
            deploy(client, endpoint)


if __name__ == "__main__":
    main()
