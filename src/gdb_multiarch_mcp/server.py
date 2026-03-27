"""MCP Server for gdb-multiarch debugging on Nintendo Switch."""

import asyncio
import importlib.resources
import json
import logging
import os
import shutil
import threading
from typing import Any, Optional
from mcp.server import Server
from mcp.types import Tool, TextContent
from pydantic import BaseModel, Field
from .gdb_interface import GDBSession

# Set up logging
log_level = os.environ.get("GDB_MCP_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Default scripts deployment directory inside WSL
SCRIPTS_DEPLOY_DIR = os.path.expanduser("~/.gdb_multiarch_mcp")

# Default Switch/Yuzu connection settings (overridable via env vars)
DEFAULT_SWITCH_IP = os.environ.get("SWITCH_IP", "192.168.1.235")
DEFAULT_SWITCH_PORT = int(os.environ.get("SWITCH_PORT", "22225"))


def _deploy_scripts() -> str:
    """
    Deploy bundled GDB scripts to a known directory and return the path.

    Copies gdbinit_switch, attach.py, and print_addr_setup.py to ~/.gdb_multiarch_mcp/
    and rewrites the {SCRIPTS_DIR} placeholder in gdbinit_switch.

    Returns:
        Path to the directory containing the deployed scripts.
    """
    os.makedirs(SCRIPTS_DEPLOY_DIR, exist_ok=True)

    scripts_pkg = importlib.resources.files("gdb_multiarch_mcp") / "scripts"

    # Copy attach.py and print_addr_setup.py
    for script_name in ("attach.py", "print_addr_setup.py"):
        src = scripts_pkg / script_name
        dst = os.path.join(SCRIPTS_DEPLOY_DIR, script_name)
        with importlib.resources.as_file(src) as src_path:
            shutil.copy2(str(src_path), dst)

    # Copy and template gdbinit_switch
    gdbinit_src = scripts_pkg / "gdbinit_switch"
    gdbinit_dst = os.path.join(SCRIPTS_DEPLOY_DIR, "gdbinit_switch")
    with importlib.resources.as_file(gdbinit_src) as src_path:
        content = src_path.read_text()
    content = content.replace("{SCRIPTS_DIR}", SCRIPTS_DEPLOY_DIR)
    with open(gdbinit_dst, "w") as f:
        f.write(content)

    logger.info(f"Deployed Switch GDB scripts to {SCRIPTS_DEPLOY_DIR}")
    return SCRIPTS_DEPLOY_DIR


# Global single session — manually started via switch_start_session
_session: Optional[GDBSession] = None
_session_lock = threading.Lock()


def _start_session() -> dict:
    """
    Start a gdb-multiarch session and connect to Yuzu.

    Uses DEFAULT_SWITCH_IP:DEFAULT_SWITCH_PORT, sources the Switch gdbinit
    commands, runs attach.py to auto-attach and set $main.
    """
    global _session
    with _session_lock:
        if _session is not None and _session.is_running:
            return {"status": "error", "message": "Session already running. Stop it first."}

        scripts_dir = _deploy_scripts()

        _session = GDBSession()
        init_cmds = [
            f"source {scripts_dir}/gdbinit_switch",
            f"target extended-remote {DEFAULT_SWITCH_IP}:{DEFAULT_SWITCH_PORT}",
            f"source {scripts_dir}/attach.py",
        ]

        result = _session.start(init_commands=init_cmds)

        if result.get("status") == "error":
            logger.error(f"Start failed: {result.get('message')}")
            _session = None

        return result


def _get_session() -> Optional[GDBSession]:
    """Return the active session, or None."""
    with _session_lock:
        if _session is not None and _session.is_running:
            return _session
        return None


def _stop_session() -> dict:
    """Stop the current session if one exists."""
    global _session
    with _session_lock:
        if _session is None:
            return {"status": "error", "message": "No active session"}
        result = _session.stop()
        _session = None
        return result


# Create MCP server instance
app = Server("gdb-multiarch-mcp")


# ── Tool argument models (no session_id — single auto-managed session) ──

class ExecuteCommandArgs(BaseModel):
    command: str = Field(..., description="GDB command to execute")


class GetBacktraceArgs(BaseModel):
    thread_id: Optional[int] = Field(None, description="Thread ID (None for current thread)")
    max_frames: int = Field(100, description="Maximum number of frames to retrieve")


class SetBreakpointArgs(BaseModel):
    location: str = Field(..., description="Breakpoint location (function, file:line, or *address)")
    condition: Optional[str] = Field(None, description="Conditional expression")
    temporary: bool = Field(False, description="Whether breakpoint is temporary")


class EvaluateExpressionArgs(BaseModel):
    expression: str = Field(..., description="C/C++ expression to evaluate")


class GetVariablesArgs(BaseModel):
    thread_id: Optional[int] = Field(None, description="Thread ID (None for current)")
    frame: int = Field(0, description="Frame number (0 is current)")


class ThreadSelectArgs(BaseModel):
    thread_id: int = Field(..., description="Thread ID to select")


class BreakpointNumberArgs(BaseModel):
    number: int = Field(..., description="Breakpoint number")


class FrameSelectArgs(BaseModel):
    frame_number: int = Field(..., description="Frame number (0 is current/innermost frame)")


class CallFunctionArgs(BaseModel):
    function_call: str = Field(
        ...,
        description="Function call expression (e.g., 'printf(\"hello\\n\")' or 'my_func(arg1, arg2)')",
    )


# ── Switch-specific tool argument models ──────────────────────────────

class OffsetArgs(BaseModel):
    offset: str = Field(
        ...,
        description="Offset into main executable (hex, e.g. '0x3a5f10' or '3a5f10')",
    )


class ReplaceArgs(BaseModel):
    offset: str = Field(..., description="Offset into main executable (hex)")
    instruction: str = Field(
        ...,
        description="New instruction as a 32-bit hex value (e.g. '0xD503201F')",
    )


class LocalizeArgs(BaseModel):
    address: str = Field(
        ...,
        description="Absolute address or register name (e.g. '0x8012345' or '$x0')",
    )


class XxdArgs(BaseModel):
    address: str = Field(..., description="Start address for hex dump")
    size: str = Field(..., description="Number of bytes to dump (hex)")


# ── Tool listing ──────────────────────────────────────────────────────

NO_ARGS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List all available GDB debugging tools."""
    return [
        # ── Session management ────────────────────────────────────
        Tool(
            name="switch_start_session",
            description=(
                "Start gdb-multiarch and connect to the Switch/Yuzu GDB stub. "
                "This MUST be called before any other tool. "
                "Connects to the IP/port configured via SWITCH_IP and SWITCH_PORT "
                "environment variables (defaults: 192.168.1.235:22225). "
                "Automatically loads Switch debug commands, attaches to the game, "
                "and sets $main to the base address of cross2_Release.nss. "
                "Do NOT manually run 'target remote', 'attach', or 'set $main' — "
                "this tool handles all of that."
            ),
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="switch_stop_session",
            description="Stop the gdb-multiarch session and disconnect from Yuzu.",
            inputSchema=NO_ARGS_SCHEMA,
        ),

        # ── Standard GDB tools ────────────────────────────────────
        Tool(
            name="gdb_execute_command",
            description=(
                "Execute a raw GDB command (CLI or MI). "
                "For Switch-specific operations, prefer the dedicated switch_* tools. "
                "IMPORTANT: Do NOT use 'target remote' or 'target extended-remote' — "
                "the session auto-connects to the Switch on first use. "
                "Do NOT use 'attach' or 'monitor wait application' — handled automatically. "
                "$main is already set to the base address of the game executable."
            ),
            inputSchema=ExecuteCommandArgs.model_json_schema(),
        ),
        Tool(
            name="gdb_get_status",
            description="Get the current status of the GDB session.",
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="gdb_get_threads",
            description="Get information about all threads.",
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="gdb_select_thread",
            description="Select a specific thread.",
            inputSchema=ThreadSelectArgs.model_json_schema(),
        ),
        Tool(
            name="gdb_get_backtrace",
            description="Get the stack backtrace (standard GDB backtrace).",
            inputSchema=GetBacktraceArgs.model_json_schema(),
        ),
        Tool(
            name="gdb_select_frame",
            description="Select a specific stack frame.",
            inputSchema=FrameSelectArgs.model_json_schema(),
        ),
        Tool(
            name="gdb_get_frame_info",
            description="Get information about the current stack frame.",
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="gdb_set_breakpoint",
            description=(
                "Set a breakpoint at a function, file:line, or *address. "
                "For offset-from-main breakpoints, use switch_break_at instead."
            ),
            inputSchema=SetBreakpointArgs.model_json_schema(),
        ),
        Tool(
            name="gdb_list_breakpoints",
            description="List all breakpoints with structured data.",
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="gdb_delete_breakpoint",
            description="Delete a breakpoint by number.",
            inputSchema=BreakpointNumberArgs.model_json_schema(),
        ),
        Tool(
            name="gdb_enable_breakpoint",
            description="Enable a breakpoint by number.",
            inputSchema=BreakpointNumberArgs.model_json_schema(),
        ),
        Tool(
            name="gdb_disable_breakpoint",
            description="Disable a breakpoint by number.",
            inputSchema=BreakpointNumberArgs.model_json_schema(),
        ),
        Tool(
            name="gdb_continue",
            description="Continue execution until next breakpoint or completion. Only use when program is PAUSED.",
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="gdb_step",
            description="Step into the next instruction.",
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="gdb_next",
            description="Step over to the next line.",
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="gdb_interrupt",
            description="Interrupt (pause) a running program.",
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="gdb_evaluate_expression",
            description="Evaluate a C/C++ expression.",
            inputSchema=EvaluateExpressionArgs.model_json_schema(),
        ),
        Tool(
            name="gdb_get_variables",
            description="Get local variables for a stack frame.",
            inputSchema=GetVariablesArgs.model_json_schema(),
        ),
        Tool(
            name="gdb_get_registers",
            description="Get CPU register values.",
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="gdb_call_function",
            description=(
                "Call a function in the target process. WARNING: executes code in the "
                "debugged program."
            ),
            inputSchema=CallFunctionArgs.model_json_schema(),
        ),

        # ── Switch-specific tools ────────────────────────────────
        Tool(
            name="switch_break_at",
            description=(
                "Set a breakpoint at an offset relative to the base of main ($main). "
                "Example: offset '0x3a5f10' sets a breakpoint at $main+0x3a5f10."
            ),
            inputSchema=OffsetArgs.model_json_schema(),
        ),
        Tool(
            name="switch_no_op",
            description=(
                "NOP the instruction at the given offset from main. "
                "Writes ARM64 NOP (0xD503201F) at $main+offset."
            ),
            inputSchema=OffsetArgs.model_json_schema(),
        ),
        Tool(
            name="switch_stub",
            description=(
                "Stub the function at the given offset from main. "
                "Writes ARM64 RET (0xD65F03C0) at $main+offset, making the function "
                "return immediately."
            ),
            inputSchema=OffsetArgs.model_json_schema(),
        ),
        Tool(
            name="switch_replace",
            description=(
                "Replace the instruction at an offset from main with a new instruction. "
                "Offset and instruction are both hex values."
            ),
            inputSchema=ReplaceArgs.model_json_schema(),
        ),
        Tool(
            name="switch_get_pc",
            description="Get the current PC as an offset relative to the base of main.",
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="switch_localize",
            description=(
                "Convert an absolute address or register value to an offset relative "
                "to the base of main. Example: address '$x0' or '0x8012345'."
            ),
            inputSchema=LocalizeArgs.model_json_schema(),
        ),
        Tool(
            name="switch_my_bt",
            description=(
                "Print the backtrace as absolute addresses by walking the frame pointer "
                "chain. Often misses the first address — use 'p/x $lr' for that."
            ),
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="switch_my_bt2",
            description=(
                "Print the backtrace with offsets relative to the base of main. "
                "Walks the frame pointer chain and resolves each return address "
                "to a module-relative offset."
            ),
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="switch_print_trace",
            description=(
                "Combined trace: runs get_pc, localize $lr, and my_bt2 in one command. "
                "Returns the current PC offset, calling offset, and full backtrace "
                "with offsets relative to main."
            ),
            inputSchema=NO_ARGS_SCHEMA,
        ),
        Tool(
            name="switch_xxd",
            description="Print a hex dump (xxd-style) of memory at the given address.",
            inputSchema=XxdArgs.model_json_schema(),
        ),
        Tool(
            name="switch_prepare_rehook",
            description=(
                "Dump the original instructions at an offset (4 instructions / 16 bytes) "
                "as 'replace' commands, so you can restore them after hooking."
            ),
            inputSchema=OffsetArgs.model_json_schema(),
        ),
    ]


# ── Tool dispatch ─────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Handle tool calls from the MCP client."""

    try:
        if name == "switch_start_session":
            result = _start_session()

        elif name == "switch_stop_session":
            result = _stop_session()

        else:
            session = _get_session()
            if session is None:
                result = {
                    "status": "error",
                    "message": "No active session. Call switch_start_session first.",
                }

            # ── Standard GDB tools ───────────────────────────────
            elif name == "gdb_execute_command":
                a = ExecuteCommandArgs(**arguments)
                result = session.execute_command(command=a.command)

            elif name == "gdb_get_status":
                result = session.get_status()

            elif name == "gdb_get_threads":
                result = session.get_threads()

            elif name == "gdb_select_thread":
                a = ThreadSelectArgs(**arguments)
                result = session.select_thread(thread_id=a.thread_id)

            elif name == "gdb_get_backtrace":
                a = GetBacktraceArgs(**arguments)
                result = session.get_backtrace(thread_id=a.thread_id, max_frames=a.max_frames)

            elif name == "gdb_select_frame":
                a = FrameSelectArgs(**arguments)
                result = session.select_frame(frame_number=a.frame_number)

            elif name == "gdb_get_frame_info":
                result = session.get_frame_info()

            elif name == "gdb_set_breakpoint":
                a = SetBreakpointArgs(**arguments)
                result = session.set_breakpoint(
                    location=a.location, condition=a.condition, temporary=a.temporary
                )

            elif name == "gdb_list_breakpoints":
                result = session.list_breakpoints()

            elif name == "gdb_delete_breakpoint":
                a = BreakpointNumberArgs(**arguments)
                result = session.delete_breakpoint(number=a.number)

            elif name == "gdb_enable_breakpoint":
                a = BreakpointNumberArgs(**arguments)
                result = session.enable_breakpoint(number=a.number)

            elif name == "gdb_disable_breakpoint":
                a = BreakpointNumberArgs(**arguments)
                result = session.disable_breakpoint(number=a.number)

            elif name == "gdb_continue":
                result = session.continue_execution()

            elif name == "gdb_step":
                result = session.step()

            elif name == "gdb_next":
                result = session.next()

            elif name == "gdb_interrupt":
                result = session.interrupt()

            elif name == "gdb_evaluate_expression":
                a = EvaluateExpressionArgs(**arguments)
                result = session.evaluate_expression(a.expression)

            elif name == "gdb_get_variables":
                a = GetVariablesArgs(**arguments)
                result = session.get_variables(thread_id=a.thread_id, frame=a.frame)

            elif name == "gdb_get_registers":
                result = session.get_registers()

            elif name == "gdb_call_function":
                a = CallFunctionArgs(**arguments)
                result = session.call_function(function_call=a.function_call)

            # ── Switch-specific tools ────────────────────────────
            elif name == "switch_break_at":
                a = OffsetArgs(**arguments)
                result = session.execute_command(f"break_at {a.offset}")

            elif name == "switch_no_op":
                a = OffsetArgs(**arguments)
                result = session.execute_command(f"no_op {a.offset}")

            elif name == "switch_stub":
                a = OffsetArgs(**arguments)
                result = session.execute_command(f"stub {a.offset}")

            elif name == "switch_replace":
                a = ReplaceArgs(**arguments)
                result = session.execute_command(f"replace {a.offset} {a.instruction}")

            elif name == "switch_get_pc":
                result = session.execute_command("get_pc")

            elif name == "switch_localize":
                a = LocalizeArgs(**arguments)
                result = session.execute_command(f"localize {a.address}")

            elif name == "switch_my_bt":
                result = session.execute_command("my_bt")

            elif name == "switch_my_bt2":
                result = session.execute_command("my_bt2")

            elif name == "switch_print_trace":
                result = session.execute_command("print_trace")

            elif name == "switch_xxd":
                a = XxdArgs(**arguments)
                result = session.execute_command(f"xxd {a.address} {a.size}")

            elif name == "switch_prepare_rehook":
                a = OffsetArgs(**arguments)
                result = session.execute_command(f"prepare_rehook {a.offset}")

            else:
                result = {"status": "error", "message": f"Unknown tool: {name}"}

        result_text = json.dumps(result, indent=2)
        return [TextContent(type="text", text=result_text)]

    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}", exc_info=True)
        error_result = {"status": "error", "message": str(e), "tool": name}
        return [TextContent(type="text", text=json.dumps(error_result, indent=2))]


async def main():
    """Main async entry point for the MCP server."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        logger.info("gdb-multiarch MCP Server starting...")
        await app.run(read_stream, write_stream, app.create_initialization_options())


def run_server():
    """Synchronous entry point for the MCP server."""
    asyncio.run(main())


if __name__ == "__main__":
    run_server()
