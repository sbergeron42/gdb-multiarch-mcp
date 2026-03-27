# gdb-multiarch-mcp

An MCP (Model Context Protocol) server that gives AI assistants like Claude direct access to `gdb-multiarch` for debugging Nintendo Switch executables running on Yuzu or a real console with a GDB stub.

Built on top of [Ipiano/gdb-mcp](https://github.com/Ipiano/gdb-mcp), this fork adds Switch-specific debugging tools for offset-based breakpoints, instruction patching (NOP/stub/replace), frame-pointer backtraces, and address localization — all relative to the game's base address (`$main`).

## What It Does

When Claude (or any MCP client) calls `switch_start_session`, the server:

1. Launches `gdb-multiarch` inside WSL
2. Loads the Switch debugging commands (`.gdbinit.switch`)
3. Connects to the GDB stub via `target extended-remote`
4. Waits for the application to launch, attaches to it
5. Automatically sets `$main` to the base address of `cross2_Release.nss`

From there, all standard GDB operations and Switch-specific tools are available through MCP tool calls.

## Prerequisites

- **Windows with WSL** — `gdb-multiarch` runs inside WSL (tested with Debian)
- **gdb-multiarch** installed in WSL (`sudo apt install gdb-multiarch`)
- **Python 3.10+** in WSL
- **A GDB stub** — either Yuzu's built-in GDB stub or a Switch with [sys-gdbstub](https://github.com/misson20000/sys-gdbstub)
- **Claude Code** (or any MCP-compatible client)

## Installation

### 1. Install gdb-multiarch in WSL

```bash
wsl -d Debian
sudo apt install gdb-multiarch
```

### 2. Install the MCP server

From Windows, run:

```bash
wsl.exe -d Debian -e bash -c 'export PATH=$HOME/.local/bin:$PATH && pip install --break-system-packages -e /mnt/c/path/to/gdb-multiarch-mcp'
```

Or from inside WSL:

```bash
pip install -e /mnt/c/path/to/gdb-multiarch-mcp
```

### 3. Add to Claude Code

```bash
claude mcp add gdb-multiarch -s user -- wsl.exe -d Debian -e bash -c "export PATH=\$HOME/.local/bin:\$PATH && python3 -m gdb_multiarch_mcp"
```

Or manually add to your `.claude.json`:

```json
{
  "mcpServers": {
    "gdb-multiarch": {
      "type": "stdio",
      "command": "wsl.exe",
      "args": [
        "-d", "Debian", "-e", "bash", "-c",
        "export PATH=$HOME/.local/bin:$PATH && python3 -m gdb_multiarch_mcp"
      ]
    }
  }
}
```

### 4. Verify

```bash
claude mcp list
```

You should see `gdb-multiarch: ... - Connected`.

## Configuration

Set these environment variables in WSL to customize the connection:

| Variable | Default | Description |
|---|---|---|
| `SWITCH_IP` | `192.168.1.235` | IP address of the Switch/Yuzu GDB stub |
| `SWITCH_PORT` | `22225` | GDB stub port |
| `GDB_PATH` | `gdb-multiarch` | Path to the gdb-multiarch binary |
| `GDB_MCP_LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

To set these, add `env` to your MCP config:

```json
{
  "mcpServers": {
    "gdb-multiarch": {
      "type": "stdio",
      "command": "wsl.exe",
      "args": ["..."],
      "env": {
        "SWITCH_IP": "192.168.1.100",
        "SWITCH_PORT": "22225"
      }
    }
  }
}
```

## Available Tools

### Session Management

| Tool | Description |
|---|---|
| `switch_start_session` | Connect to the Switch/Yuzu, attach to the game, set `$main`. **Call this first.** |
| `switch_stop_session` | Disconnect and clean up |

### Switch Debugging Tools

| Tool | Description |
|---|---|
| `switch_break_at` | Set breakpoint at `$main+offset` |
| `switch_no_op` | NOP instruction at offset (writes `0xD503201F`) |
| `switch_stub` | Stub function at offset (writes `RET` / `0xD65F03C0`) |
| `switch_replace` | Replace instruction at offset with arbitrary value |
| `switch_get_pc` | Get PC as offset relative to `$main` |
| `switch_localize` | Convert absolute address to offset relative to `$main` |
| `switch_my_bt` | Backtrace as absolute addresses (frame pointer walk) |
| `switch_my_bt2` | Backtrace with offsets relative to `$main` |
| `switch_print_trace` | Combined: PC offset + LR offset + full backtrace |
| `switch_xxd` | Hex dump of memory |
| `switch_prepare_rehook` | Dump 4 original instructions at offset for later restore |

### Standard GDB Tools

All standard `gdb-mcp` tools are also available:

| Tool | Description |
|---|---|
| `gdb_execute_command` | Execute any GDB command (CLI or MI) |
| `gdb_set_breakpoint` | Set breakpoint at function/file:line/address |
| `gdb_list_breakpoints` | List all breakpoints |
| `gdb_delete_breakpoint` | Delete breakpoint by number |
| `gdb_enable_breakpoint` | Enable breakpoint |
| `gdb_disable_breakpoint` | Disable breakpoint |
| `gdb_continue` | Continue execution |
| `gdb_step` | Step into |
| `gdb_next` | Step over |
| `gdb_interrupt` | Pause running program |
| `gdb_get_backtrace` | Standard GDB backtrace |
| `gdb_get_threads` | List threads |
| `gdb_select_thread` | Switch to thread |
| `gdb_select_frame` | Select stack frame |
| `gdb_get_frame_info` | Current frame info |
| `gdb_evaluate_expression` | Evaluate C/C++ expression |
| `gdb_get_variables` | Local variables for a frame |
| `gdb_get_registers` | CPU register values |
| `gdb_call_function` | Call function in target process |
| `gdb_get_status` | Session status |

## Troubleshooting

### "No route to host" when connecting

WSL networking can be tricky. Try:

1. **Confirm SSH is running in WSL**: `sudo service ssh start`
2. **Test connectivity**: `nc -vz <switch_ip> 22225`
3. **Add a route if needed**: `sudo ip route add 192.168.1.0/24 via <gateway_ip>`
4. **Port proxy from Windows PowerShell**:
   ```powershell
   netsh interface portproxy add v4tov4 listenport=22225 listenaddress=127.0.0.1 connectport=22225 connectaddress=<switch_ip>
   ```

### GDB stub not responding

- Make sure the game is running on Yuzu/Switch **before** calling `switch_start_session`
- Verify Yuzu's GDB stub is enabled in `Emulation > Configure > Debug > Enable GDB Stub`

### Session already running

Call `switch_stop_session` first, then `switch_start_session` again.

## Credits

- **[Ipiano/gdb-mcp](https://github.com/Ipiano/gdb-mcp)** by Andrew Stelter — the upstream MCP server for GDB that this project is built on
- **[Coolsonickirby/smash-ultimate-research-setup](https://github.com/Coolsonickirby/smash-ultimate-research-setup)** — the modified `.gdbinit.switch` and `attach.py` (auto-attach script) used in this project
- **[blujay](https://twitter.com/jayblu_/)** — the original `.gdbinit.switch` commands
- **CookieScythe** — `print_addr_setup.py` (address-to-offset resolution)
- **[Gdbinit](https://github.com/gdbinit/Gdbinit)** by mammon_, elaine, pusillus, mong, zhang le, l0kit, truthix, fG!, gln — the extended `.gdbinit` configuration

## License

MIT — see [LICENSE](LICENSE).
