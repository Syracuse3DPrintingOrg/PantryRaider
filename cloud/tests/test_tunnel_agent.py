"""The VPS tunnel agent's pure helpers: the Caddy include renderer, the peer
store transforms, and the token check. The agent is a standalone script with
no .py extension (it installs to /usr/local/bin), so it is loaded here by
path. No wg or Caddy is invoked; the command runner is never called by these
pure helpers."""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_AGENT_PATH = Path(__file__).parent.parent / "vps" / "forager-tunnel-agent"


def _load_agent():
    # The agent installs as an extensionless script, so give importlib an
    # explicit source loader rather than relying on suffix detection.
    loader = SourceFileLoader("forager_tunnel_agent", str(_AGENT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


agent = _load_agent()

# Real-shaped WireGuard public keys (44 characters of base64).
KEYX = "X" * 43 + "="
KEYS = "S" * 43 + "="


def test_render_kitchen_block():
    block = agent.render_kitchen_block("kitchen-pi.forager.pantryraider.app",
                                       "10.99.0.2")
    assert "kitchen-pi.forager.pantryraider.app {" in block
    assert "reverse_proxy 10.99.0.2:9284" in block


def test_render_kitchen_block_uses_given_app_port():
    # A server runs WireGuard in the app container; Caddy reaches it on 8000.
    block = agent.render_kitchen_block("srv.forager.pantryraider.app",
                                       "10.99.0.5", 8000)
    assert "reverse_proxy 10.99.0.5:8000" in block


def test_render_caddy_config_is_sorted_and_headed():
    peers = [
        {"public_key": "B", "tunnel_ip": "10.99.0.3", "domain": "zed.example"},
        {"public_key": "A", "tunnel_ip": "10.99.0.2", "domain": "abc.example"},
    ]
    out = agent.render_caddy_config(peers)
    assert out.startswith("# Managed by forager-tunnel-agent")
    # Sorted by domain so the file is stable across dict orderings.
    assert out.index("abc.example") < out.index("zed.example")
    assert "reverse_proxy 10.99.0.2:9284" in out
    assert "reverse_proxy 10.99.0.3:9284" in out


def test_render_caddy_config_empty_is_just_the_header():
    out = agent.render_caddy_config([])
    assert out.startswith("# Managed by forager-tunnel-agent")
    assert "reverse_proxy" not in out


def test_render_caddy_config_uses_per_peer_app_port():
    # A server peer carries its own app_port; a Pi peer without the field falls
    # back to the 9284 default, so peers stored before app_port existed still
    # render correctly.
    peers = [
        {"public_key": "S", "tunnel_ip": "10.99.0.5",
         "domain": "srv.example", "app_port": 8000},
        {"public_key": "P", "tunnel_ip": "10.99.0.2", "domain": "pi.example"},
    ]
    out = agent.render_caddy_config(peers)
    assert "reverse_proxy 10.99.0.5:8000" in out
    assert "reverse_proxy 10.99.0.2:9284" in out


def test_render_caddy_config_skips_incomplete_peers():
    peers = [
        {"public_key": "A", "tunnel_ip": "10.99.0.2", "domain": ""},
        {"public_key": "B", "tunnel_ip": "", "domain": "b.example"},
        {"public_key": "C", "tunnel_ip": "10.99.0.4", "domain": "c.example"},
    ]
    out = agent.render_caddy_config(peers)
    assert "c.example" in out
    assert out.count("reverse_proxy") == 1


def test_upsert_peer_adds_and_updates():
    peers = []
    peers = agent.upsert_peer(peers, "K1", "10.99.0.2", "a.example")
    assert len(peers) == 1
    # A default port is recorded when none is given.
    assert peers[0]["app_port"] == 9284
    # Updating the same key replaces the entry rather than duplicating it, and
    # carries the new app_port.
    peers = agent.upsert_peer(peers, "K1", "10.99.0.9", "a.example", 8000)
    assert len(peers) == 1
    assert peers[0]["tunnel_ip"] == "10.99.0.9"
    assert peers[0]["app_port"] == 8000


def test_remove_peer_entry():
    peers = [{"public_key": "K1", "tunnel_ip": "10.99.0.2", "domain": "a"},
             {"public_key": "K2", "tunnel_ip": "10.99.0.3", "domain": "b"}]
    out = agent.remove_peer_entry(peers, "K1")
    assert [p["public_key"] for p in out] == ["K2"]


def test_token_ok():
    assert agent.token_ok("secret", "secret") is True
    assert agent.token_ok("secret", "other") is False
    assert agent.token_ok("", "secret") is False
    # An empty expected token refuses everything (never runs open).
    assert agent.token_ok("secret", "") is False


def test_apply_add_shells_out_without_a_shell(tmp_path, monkeypatch):
    """apply_add drives wg, wg-quick, the state file, the Caddy include, and a
    reload, all as arg lists (never shell=True). The runner is recorded."""
    ran = []
    monkeypatch.setattr(agent, "run_command", lambda args: ran.append(args))
    monkeypatch.setattr(agent, "PEERS_STATE", str(tmp_path / "peers.json"))
    monkeypatch.setattr(agent, "CADDY_KITCHENS_FILE",
                        str(tmp_path / "kitchens.caddy"))

    agent.apply_add(KEYX, "10.99.0.2", "kitchen-pi.forager.pantryraider.app")

    # wg set, wg-quick save, systemctl reload caddy, each an arg list.
    assert ["wg", "set", "wg0", "peer", KEYX, "allowed-ips", "10.99.0.2/32",
            "persistent-keepalive", "25"] in ran
    assert ["wg-quick", "save", "wg0"] in ran
    assert ["systemctl", "reload", "caddy"] in ran
    # The Caddy include now carries the kitchen, on the default 9284.
    written = (tmp_path / "kitchens.caddy").read_text()
    assert "kitchen-pi.forager.pantryraider.app" in written
    assert "reverse_proxy 10.99.0.2:9284" in written


def test_apply_add_routes_a_server_to_its_internal_port(tmp_path, monkeypatch):
    """A server passes its in-container port, and Caddy is pointed at it."""
    monkeypatch.setattr(agent, "run_command", lambda args: None)
    monkeypatch.setattr(agent, "PEERS_STATE", str(tmp_path / "peers.json"))
    monkeypatch.setattr(agent, "CADDY_KITCHENS_FILE",
                        str(tmp_path / "kitchens.caddy"))
    agent.apply_add(KEYS, "10.99.0.5", "srv.forager.pantryraider.app", 8000)
    written = (tmp_path / "kitchens.caddy").read_text()
    assert "reverse_proxy 10.99.0.5:8000" in written


def test_apply_remove_drops_the_peer(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(agent, "run_command", lambda args: ran.append(args))
    monkeypatch.setattr(agent, "PEERS_STATE", str(tmp_path / "peers.json"))
    monkeypatch.setattr(agent, "CADDY_KITCHENS_FILE",
                        str(tmp_path / "kitchens.caddy"))
    agent.apply_add(KEYX, "10.99.0.2", "kitchen-pi.example")
    agent.apply_remove(KEYX)
    assert ["wg", "set", "wg0", "peer", KEYX, "remove"] in ran
    assert "reverse_proxy" not in (tmp_path / "kitchens.caddy").read_text()


# --- Input checks and safe Caddy changes -----------------------------------

def test_input_validators():
    assert agent.valid_public_key(KEYX)
    assert not agent.valid_public_key("KEYX")
    assert not agent.valid_public_key("A" * 44)
    assert not agent.valid_public_key("A" * 42 + "}=")
    assert not agent.valid_public_key(KEYX + "\n")
    assert agent.valid_tunnel_ip("10.99.0.2")
    assert not agent.valid_tunnel_ip("10.99.0.1")      # the server itself
    assert not agent.valid_tunnel_ip("10.99.0.0")
    assert not agent.valid_tunnel_ip("192.168.1.5")    # outside the tunnel net
    assert not agent.valid_tunnel_ip("10.99.0.2:80")
    assert agent.valid_domain("kitchen-pi.forager.pantryraider.app")
    assert not agent.valid_domain("x.example {\n\trespond hi\n}")
    assert not agent.valid_domain("UPPER.example")
    assert not agent.valid_domain("nodot")
    assert not agent.valid_domain("a.example\n")
    assert not agent.valid_domain("-bad.example")
    assert agent.valid_app_port(8000)
    for bad in (0, 65536, 99999, "80", True):
        assert not agent.valid_app_port(bad)


def test_upsert_peer_replaces_a_reused_domain_or_address():
    peers = agent.upsert_peer([], "K1", "10.99.0.2", "pr.example")
    # A new key on the same domain replaces the old entry.
    peers = agent.upsert_peer(peers, "K2", "10.99.0.3", "pr.example")
    assert [p["public_key"] for p in peers] == ["K2"]
    # A new key on the same address replaces it too.
    peers = agent.upsert_peer(peers, "K3", "10.99.0.3", "other.example")
    assert [p["public_key"] for p in peers] == ["K3"]
    out = agent.render_caddy_config(
        agent.upsert_peer(peers, "K4", "10.99.0.4", "other.example"))
    assert out.count("other.example {") == 1


def test_displaced_keys():
    peers = [{"public_key": "K1", "tunnel_ip": "10.99.0.2", "domain": "a.example"},
             {"public_key": "K2", "tunnel_ip": "10.99.0.3", "domain": "b.example"},
             {"public_key": "K3", "tunnel_ip": "10.99.0.4", "domain": "c.example"}]
    assert agent.displaced_keys(peers, "K9", "10.99.0.3", "a.example") == ["K1", "K2"]
    # Updating your own entry displaces nobody.
    assert agent.displaced_keys(peers, "K1", "10.99.0.2", "a.example") == []


def test_render_skips_entries_that_would_break_caddy():
    peers = [{"public_key": "A", "tunnel_ip": "10.99.0.2",
              "domain": "ok.example"},
             {"public_key": "B", "tunnel_ip": "10.99.0.3",
              "domain": "bad.example", "app_port": 99999},
             {"public_key": "C", "tunnel_ip": "10.99.0.4",
              "domain": "x.example}\n:80 {"}]
    out = agent.render_caddy_config(peers)
    assert out.count("reverse_proxy") == 1
    assert "99999" not in out


def _agent_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "PEERS_STATE", str(tmp_path / "peers.json"))
    monkeypatch.setattr(agent, "CADDY_KITCHENS_FILE",
                        str(tmp_path / "kitchens.caddy"))


def test_apply_add_validates_before_touching_wireguard(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(agent, "run_command", lambda args: ran.append(args))
    _agent_paths(tmp_path, monkeypatch)
    agent.apply_add(KEYX, "10.99.0.2", "kitchen-pi.example")
    validate = [a for a in ran if a[:2] == ["caddy", "validate"]]
    assert validate and "--config" in validate[0]
    assert ran.index(validate[0]) < ran.index(
        ["wg", "set", "wg0", "peer", KEYX, "allowed-ips", "10.99.0.2/32",
         "persistent-keepalive", "25"])


def test_apply_add_rolls_back_when_caddy_rejects_the_config(tmp_path,
                                                            monkeypatch):
    ran = []
    monkeypatch.setattr(agent, "run_command", lambda args: ran.append(args))
    _agent_paths(tmp_path, monkeypatch)
    agent.apply_add(KEYX, "10.99.0.2", "good.example")
    before_include = (tmp_path / "kitchens.caddy").read_text()
    before_state = (tmp_path / "peers.json").read_text()

    def reject(args):
        ran.append(args)
        if args[:2] == ["caddy", "validate"]:
            raise agent.subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(agent, "run_command", reject)
    ran.clear()
    try:
        agent.apply_add(KEYS, "10.99.0.3", "second.example")
    except agent.subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError("a rejected config must fail the add")
    # The include and the state are exactly as they were, and neither wg nor
    # a reload was attempted.
    assert (tmp_path / "kitchens.caddy").read_text() == before_include
    assert (tmp_path / "peers.json").read_text() == before_state
    assert not [a for a in ran if a[0] in ("wg", "wg-quick", "systemctl")]


def test_apply_add_restores_the_include_when_reload_fails(tmp_path, monkeypatch):
    _agent_paths(tmp_path, monkeypatch)

    def reload_fails(args):
        if args[:2] == ["systemctl", "reload"]:
            raise agent.subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(agent, "run_command", reload_fails)
    try:
        agent.apply_add(KEYX, "10.99.0.2", "good.example")
    except agent.subprocess.CalledProcessError:
        pass
    # There was no include before, so there is none after, and nothing saved.
    assert not (tmp_path / "kitchens.caddy").exists()
    assert not (tmp_path / "peers.json").exists()


def test_apply_add_removes_a_displaced_wireguard_peer(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(agent, "run_command", lambda args: ran.append(args))
    _agent_paths(tmp_path, monkeypatch)
    agent.apply_add(KEYX, "10.99.0.2", "pr.example")
    ran.clear()
    agent.apply_add(KEYS, "10.99.0.2", "pr.example")
    assert ["wg", "set", "wg0", "peer", KEYX, "remove"] in ran
    written = (tmp_path / "kitchens.caddy").read_text()
    assert written.count("pr.example {") == 1


def test_apply_remove_of_a_legacy_bad_key_only_drops_the_entry(tmp_path,
                                                               monkeypatch):
    ran = []
    monkeypatch.setattr(agent, "run_command", lambda args: ran.append(args))
    _agent_paths(tmp_path, monkeypatch)
    agent.save_peers([{"public_key": "legacy", "tunnel_ip": "10.99.0.2",
                       "domain": "old.example", "app_port": 9284}])
    agent.apply_remove("legacy")
    assert agent.load_peers() == []
    assert not [a for a in ran if a[0] == "wg"]
