"""Windows-only TCP bridge for Docker Desktop containers that cannot reach LAN Tuya plugs.

The relay binds locally, accepts only Docker-private source networks, and forwards raw
encrypted Tuya TCP bytes to the configured LAN device. It holds no Tuya credentials.
"""

import asyncio
import ipaddress
import json
import logging
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("relay-config.json")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).with_name("relay.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


def load_config() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config.setdefault("bind_host", "0.0.0.0")
    config.setdefault("allowed_networks", ["192.168.65.0/24", "172.16.0.0/12"])
    config.setdefault("routes", [])
    return config


async def bridge(client_reader, client_writer, target_host: str, target_port: int):
    peer = client_writer.get_extra_info("peername")
    try:
        server_reader, server_writer = await asyncio.wait_for(
            asyncio.open_connection(target_host, target_port), timeout=5
        )
        logging.info("Accepted relay %s -> %s:%s", peer, target_host, target_port)

        async def copy(source, destination):
            while data := await source.read(4096):
                destination.write(data)
                await destination.drain()

        a = asyncio.create_task(copy(client_reader, server_writer))
        b = asyncio.create_task(copy(server_reader, client_writer))
        await asyncio.wait((a, b), return_when=asyncio.FIRST_COMPLETED)
        for task in (a, b):
            task.cancel()
        await asyncio.gather(a, b, return_exceptions=True)
    except Exception as exc:
        logging.info("Relay %s to %s:%s failed: %s", peer, target_host, target_port, exc)
    finally:
        client_writer.close()
        await client_writer.wait_closed()


async def main():
    config = load_config()
    allowed = [ipaddress.ip_network(value) for value in config["allowed_networks"]]
    servers = []
    for route in config["routes"]:
        listen_port, target_host, target_port = (
            int(route["listen_port"]),
            route["target_host"],
            int(route.get("target_port", 6668)),
        )

        async def handler(reader, writer, target_host=target_host, target_port=target_port):
            peer = writer.get_extra_info("peername")
            peer_ip = ipaddress.ip_address(peer[0]) if peer else None
            if not peer_ip or not any(peer_ip in network for network in allowed):
                logging.warning("Rejected non-Docker relay peer: %s", peer)
                writer.close()
                await writer.wait_closed()
                return
            await bridge(reader, writer, target_host, target_port)

        server = await asyncio.start_server(handler, config["bind_host"], listen_port)
        servers.append(server)
        logging.info(
            "Relay listening on %s:%s -> %s:%s",
            config["bind_host"],
            listen_port,
            target_host,
            target_port,
        )
    await asyncio.gather(*(server.serve_forever() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
