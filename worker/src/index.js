const OWNER = "YOUR_GITHUB_OWNER";
const REPOSITORY = "YOUR_PRIVATE_RUNTIME_REPOSITORY";
const WORKFLOW = "codex-window-primer.yml";

function dispatchUrl() {
  if (OWNER.startsWith("YOUR_") || REPOSITORY.startsWith("YOUR_")) {
    throw new Error("Configure the private GitHub runtime repository before deployment");
  }
  return `https://api.github.com/repos/${OWNER}/${REPOSITORY}/actions/workflows/${WORKFLOW}/dispatches`;
}

export async function dispatchWorkflow(env, fetchImpl = globalThis.fetch) {
  if (!env?.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN is not configured");
  const response = await fetchImpl(dispatchUrl(), {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "codex-window-primer-scheduler",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main", inputs: { source: "cloudflare" } }),
  });
  if (!response.ok) {
    throw new Error(`GitHub workflow dispatch failed with status ${response.status}`);
  }
}

export default {
  async fetch() {
    return new Response("codex-window-primer-scheduler is running", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
  async scheduled(_controller, env) {
    await dispatchWorkflow(env);
  },
};
