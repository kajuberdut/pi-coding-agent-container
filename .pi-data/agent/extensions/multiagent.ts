/**
 * GVS5H Ledger-Based Multi-Agent Extension for Pi 1.0+
 *
 * Provides:
 *  - `multiagent` tool: Allows Pi to launch, monitor, or stop a ledger-based
 *    multi-agent workflow.
 *  - `/multiagent` command: Start or inspect background runs directly from the TUI.
 *  - Automatic mid-conversation wake-up: Polls active `--bg` runs in the background
 *    and invokes `pi.sendMessage(..., { triggerTurn: true })` the moment a background
 *    multi-agent workflow completes.
 */

import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { promisify } from "node:util";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";

const execFileAsync = promisify(execFile);

interface WatchedRun {
	runId: string;
	workspace: string;
	promptSnippet: string;
}

function getScriptPath(): string {
	const home = process.env.HOME || os.homedir() || "/home/node";
	return path.join(home, ".pi", "agent", "skills", "multiagent", "scripts", "multiagent.py");
}

async function runMultiAgentCli(args: string[]): Promise<{ stdout: string; stderr: string }> {
	const script = getScriptPath();
	return await execFileAsync("python3", [script, ...args], {
		maxBuffer: 10 * 1024 * 1024,
	});
}

export default function (pi: ExtensionAPI) {
	const watchedRuns = new Map<string, WatchedRun>();
	let pollTimer: ReturnType<typeof setInterval> | null = null;
	let latestCtx: ExtensionContext | null = null;

	function stopPollerIfIdle() {
		if (watchedRuns.size === 0 && pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
			if (latestCtx?.hasUI) {
				latestCtx.ui.setStatus("multiagent", undefined);
			}
		}
	}

	function checkWatchedRuns() {
		for (const [runId, info] of watchedRuns.entries()) {
			const statusFile = path.join(info.workspace, "status.json");
			if (!fs.existsSync(statusFile)) continue;

			try {
				const st = JSON.parse(fs.readFileSync(statusFile, "utf-8"));
				const state: string = st.state || "running";
				const phase: string = st.phase || "?";
				const iter = `${st.iteration ?? 0}/${st.max_iters ?? "?"}`;

				if (state === "running") {
					if (latestCtx?.hasUI) {
						latestCtx.ui.setStatus("multiagent", `multiagent[${runId}]: ${phase} (${iter})`);
					}
					continue;
				}

				// Run reached a terminal state (done, failed, stopped)
				watchedRuns.delete(runId);
				stopPollerIfIdle();

				let artifactPreview = "";
				if (st.artifact_file && fs.existsSync(st.artifact_file)) {
					const raw = fs.readFileSync(st.artifact_file, "utf-8");
					artifactPreview =
						raw.length > 8000 ? `${raw.slice(0, 8000)}\n...[truncated, see ${st.artifact_file}]` : raw;
				}

				const summaryText =
					state === "done"
						? `[Background Multi-Agent Run '${runId}' COMPLETED]\n` +
						  `Workspace: ${info.workspace}\n` +
						  `Model: ${st.model} (${st.endpoint})\n` +
						  `Iterations: ${iter}\n` +
						  `Artifact File: ${st.artifact_file || "(none)"}\n\n` +
						  (artifactPreview ? `### Final Artifact\n\`\`\`\n${artifactPreview}\n\`\`\`` : "")
						: `[Background Multi-Agent Run '${runId}' ended with state='${state}']\n` +
						  `Workspace: ${info.workspace}\n` +
						  (st.error ? `Error: ${st.error}` : "");

				if (latestCtx?.hasUI) {
					latestCtx.ui.notify(`Multi-agent run '${runId}' finished (${state})`, state === "done" ? "info" : "warning");
				}

				// Wake Pi up mid-conversation with the completed multi-agent output
				pi.sendMessage(
					{
						customType: "multiagent-result",
						content: summaryText,
						display: true,
						details: {
							runId,
							state,
							workspace: info.workspace,
							artifactFile: st.artifact_file,
						},
					},
					{ triggerTurn: true, deliverAs: "followUp" },
				);
			} catch {
				// Ignore transient JSON read errors while status.json is being written
			}
		}
	}

	function watchRun(runId: string, workspace: string, promptSnippet: string, ctx: ExtensionContext) {
		latestCtx = ctx;
		watchedRuns.set(runId, { runId, workspace, promptSnippet });
		if (ctx.hasUI) {
			ctx.ui.setStatus("multiagent", `multiagent[${runId}]: starting...`);
		}
		if (pollTimer === null) {
			pollTimer = setInterval(checkWatchedRuns, 2500);
		}
	}

	pi.on("session_shutdown", () => {
		if (pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
		watchedRuns.clear();
	});

	// Register the model-callable `multiagent` tool
	const multiagentTool = defineTool({
		name: "multiagent",
		label: "Multi-Agent Ledger Orchestrator",
		description:
			"Run or manage the GVS5H ledger-based multi-agent workflow (Primary Manager + Ideation & Task Workers). " +
			"Use action='start' with async=true to run " +
			"in the background and receive an automatic follow-up message when it finishes.",
		parameters: Type.Object({
			action: Type.Union(
				[
					Type.Literal("start"),
					Type.Literal("status"),
					Type.Literal("stop"),
					Type.Literal("probe"),
				],
				{ description: "Operation to perform: 'start', 'status', 'stop', or 'probe'" },
			),
			prompt: Type.Optional(
				Type.String({ description: "Problem statement or task description (required for action='start')" }),
			),
			kind: Type.Optional(
				Type.Union([Type.Literal("code"), Type.Literal("text")], {
					description: "Artifact type: 'code' (solution.py) or 'text' (answer.md). Default: 'code'",
				}),
			),
			async: Type.Optional(
				Type.Boolean({
					description: "Run asynchronously in the background and notify Pi on completion (default: true)",
				}),
			),
			runId: Type.Optional(Type.String({ description: "Optional identifier for the run" })),
			testCmd: Type.Optional(
				Type.String({
					description: "Optional verification shell command (e.g. 'pytest -q' or 'python3 test.py {solution}')",
				}),
			),
			outPath: Type.Optional(Type.String({ description: "Optional destination path to write the final artifact" })),
			apiBase: Type.Optional(Type.String({ description: "Optional override for OpenAI-compatible API base URL" })),
			model: Type.Optional(Type.String({ description: "Optional override for model ID" })),
			maxIters: Type.Optional(Type.Number({ description: "Maximum worker iterations (default: 10)" })),
		}),

		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			latestCtx = ctx;

			if (params.action === "probe") {
				const cliArgs = ["--probe"];
				if (params.apiBase) cliArgs.push("--api-base", params.apiBase);
				if (params.model) cliArgs.push("--model", params.model);
				const { stdout } = await runMultiAgentCli(cliArgs);
				return {
					content: [{ type: "text", text: stdout.trim() }],
					details: JSON.parse(stdout),
				};
			}

			if (params.action === "status") {
				const cliArgs = ["--status"];
				if (params.runId) cliArgs.push(params.runId);
				const { stdout } = await runMultiAgentCli(cliArgs);
				return {
					content: [{ type: "text", text: stdout.trim() }],
					details: { runId: params.runId },
				};
			}

			if (params.action === "stop") {
				if (!params.runId) {
					throw new Error("runId is required when action='stop'");
				}
				watchedRuns.delete(params.runId);
				stopPollerIfIdle();
				const { stdout } = await runMultiAgentCli(["--stop", params.runId]);
				return {
					content: [{ type: "text", text: stdout.trim() }],
					details: { stopped: params.runId },
				};
			}

			// action === "start"
			if (!params.prompt?.trim()) {
				throw new Error("prompt is required when action='start'");
			}

			const isAsync = params.async !== false;
			const cliArgs: string[] = [];
			if (isAsync) cliArgs.push("--bg");
			if (params.kind) cliArgs.push("--kind", params.kind);
			if (params.runId) cliArgs.push("--run-id", params.runId);
			if (params.testCmd) cliArgs.push("--test-cmd", params.testCmd);
			if (params.outPath) cliArgs.push("--out", params.outPath);
			if (params.apiBase) cliArgs.push("--api-base", params.apiBase);
			if (params.model) cliArgs.push("--model", params.model);
			if (params.maxIters) cliArgs.push("--max-iters", String(params.maxIters));
			cliArgs.push(params.prompt);

			const { stdout } = await runMultiAgentCli(cliArgs);

			if (isAsync) {
				const info = JSON.parse(stdout);
				watchRun(info.run_id, info.workspace, params.prompt.slice(0, 60), ctx);
				return {
					content: [
						{
							type: "text",
							text:
								`Started background multi-agent run '${info.run_id}' (PID ${info.pid}).\n` +
								`Workspace: ${info.workspace}\n` +
								`Pi will automatically receive a notification message when the run finishes.`,
						},
					],
					details: info,
				};
			}

			return {
				content: [{ type: "text", text: stdout.trim() }],
				details: { sync: true },
			};
		},
	});

	pi.registerTool(multiagentTool);

	// Register `/multiagent` slash command for direct interactive TUI use
	pi.registerCommand("multiagent", {
		description: "Launch or inspect a background GVS5H multi-agent run (usage: /multiagent [status|probe|stop <id>|<prompt>])",
		handler: async (argText, ctx) => {
			latestCtx = ctx;
			const trimmed = (argText || "").trim();

			if (!trimmed || trimmed === "status") {
				const { stdout } = await runMultiAgentCli(["--status"]);
				if (ctx.hasUI) {
					ctx.ui.notify(stdout.trim() || "No multiagent runs found.", "info");
				}
				return;
			}

			if (trimmed === "probe") {
				const { stdout } = await runMultiAgentCli(["--probe"]);
				if (ctx.hasUI) {
					ctx.ui.notify(stdout.trim(), "info");
				}
				return;
			}

			if (trimmed.startsWith("stop ")) {
				const id = trimmed.slice(5).trim();
				watchedRuns.delete(id);
				stopPollerIfIdle();
				const { stdout } = await runMultiAgentCli(["--stop", id]);
				if (ctx.hasUI) {
					ctx.ui.notify(stdout.trim(), "info");
				}
				return;
			}

			const { stdout } = await runMultiAgentCli(["--bg", trimmed]);
			const info = JSON.parse(stdout);
			watchRun(info.run_id, info.workspace, trimmed.slice(0, 60), ctx);
			if (ctx.hasUI) {
				ctx.ui.notify(
					`Started background multi-agent run '${info.run_id}' -> ${info.workspace}`,
					"info",
				);
			}
		},
	});
}
