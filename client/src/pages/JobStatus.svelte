<script>
  import { onMount, onDestroy } from 'svelte';
  import { token } from '../stores/auth.js';
  import { WS_BASE, API_BASE } from '../stores/api.js';
  import { push } from 'svelte-spa-router';
  import { Button } from '$lib/components/ui/button';
  import { Badge } from '$lib/components/ui/badge';
  import * as Card from '$lib/components/ui/card';
  import AskHumanDialog from '../components/AskHumanDialog.svelte';

  export let params = {};

  let jobId = params.id;
  let status = 'connecting';
  let steps = [];
  let activeToolName = null;
  let error = null;
  let terminateReason = null;
  let outputFiles = [];
  let askQuestion = null;
  let askQueue = [];
  let ws = null;

  const TOOL_LABELS = {
    learn_business_logic: 'Learning business logic',
    identify_sheets: 'Identifying sheets',
    map_columns: 'Mapping columns',
    ask_human: 'Asking for input',
    generate_code: 'Generating code',
    execute_code: 'Executing script',
    validate_output: 'Validating output',
    fix_code: 'Fixing code',
    finish: 'Done',
    terminate: 'Terminated',
  };

  const STATUS_VARIANT = {
    connecting: 'secondary',
    pending: 'secondary',
    running: 'default',
    completed: 'default',
    terminated: 'outline',
    failed: 'destructive',
    disconnected: 'outline',
  };

  onMount(() => {
    const wsUrl = `${WS_BASE}/agent/ws/${jobId}?token=${$token}`;
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      status = 'pending';
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);

      if (msg.type === 'status') {
        status = msg.status;
        if (msg.tool) {
          const last = steps[steps.length - 1];
          if (!last || last.tool !== msg.tool) {
            steps = [...steps, { step: msg.step, tool: msg.tool, success: msg.success }];
          } else {
            steps = [...steps.slice(0, -1), { ...last, success: msg.success }];
          }
        }
      } else if (msg.type === 'tool_start') {
        status = 'running';
        activeToolName = msg.tool;
      } else if (msg.type === 'tool_done') {
        status = 'running';
        activeToolName = null;
        const last = steps[steps.length - 1];
        if (!last || last.tool !== msg.tool) {
          steps = [...steps, { step: msg.step, tool: msg.tool, success: msg.success }];
        } else {
          steps = [...steps.slice(0, -1), { ...last, success: msg.success }];
        }
      } else if (msg.type === 'ask_human') {
        askQueue = [...askQueue, msg.question];
        if (!askQuestion) {
          askQuestion = askQueue.shift();
          askQueue = [...askQueue];
        }
      } else if (msg.type === 'completed') {
        status = 'completed';
        activeToolName = null;
        outputFiles = msg.output_files || [];
      } else if (msg.type === 'terminated') {
        status = 'terminated';
        activeToolName = null;
        terminateReason = msg.reason;
      } else if (msg.type === 'failed') {
        status = 'failed';
        activeToolName = null;
        error = msg.error;
      }
    };

    ws.onclose = () => {
      if (status !== 'completed' && status !== 'failed' && status !== 'terminated') {
        status = 'disconnected';
      }
    };
  });

  onDestroy(() => {
    if (ws) ws.close();
  });

  function handleHumanResponse(e) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'human_response', answer: e.detail.answer }));
    }
    // Show next queued question, or clear
    if (askQueue.length > 0) {
      askQuestion = askQueue.shift();
      askQueue = [...askQueue];
    } else {
      askQuestion = null;
    }
  }

  function killAgent() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'kill' }));
    }
  }

  function downloadOutput(filePath) {
    fetch(`${API_BASE}/agent/jobs/${jobId}/output`, {
      headers: { Authorization: `Bearer ${$token}` },
    })
      .then((res) => res.blob())
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filePath.split('/').pop() || 'output.xlsx';
        a.click();
        URL.revokeObjectURL(url);
      });
  }
</script>

<div class="flex justify-center pt-12">
  <Card.Root class="w-full max-w-xl">
    <Card.Header>
      <div class="flex items-center justify-between">
        <Card.Title>Agent Run</Card.Title>
        <Badge variant={STATUS_VARIANT[status] || 'secondary'}>
          {status}
        </Badge>
      </div>
      <Card.Description class="font-mono text-xs">Job: {jobId}</Card.Description>
    </Card.Header>
    <Card.Content class="space-y-4">
      <!-- Steps timeline -->
      <div class="space-y-1">
        {#each steps as step}
          <div class="flex items-center gap-3 rounded-md px-3 py-2
            {step.success === false ? 'bg-destructive/5' : 'bg-muted/50'}">
            <div class="h-2.5 w-2.5 flex-shrink-0 rounded-full
              {step.success === false ? 'bg-destructive' : step.success ? 'bg-green-500' : 'bg-muted-foreground'}">
            </div>
            <span class="flex-1 text-sm">{TOOL_LABELS[step.tool] || step.tool}</span>
            <span class="text-xs text-muted-foreground">Step {step.step}</span>
          </div>
        {/each}

        {#if activeToolName}
          <div class="flex items-center gap-3 rounded-md bg-primary/5 px-3 py-2">
            <div class="h-2.5 w-2.5 flex-shrink-0 animate-pulse rounded-full bg-primary"></div>
            <span class="flex-1 text-sm">{TOOL_LABELS[activeToolName] || activeToolName}...</span>
          </div>
        {:else if status === 'running'}
          <div class="flex items-center gap-3 rounded-md bg-primary/5 px-3 py-2">
            <div class="h-2.5 w-2.5 flex-shrink-0 animate-pulse rounded-full bg-primary"></div>
            <span class="flex-1 text-sm text-muted-foreground">Thinking...</span>
          </div>
        {/if}
      </div>

      <!-- Kill button while running -->
      {#if status === 'running' || status === 'pending'}
        <Button variant="destructive" class="w-full" onclick={killAgent}>
          Kill Agent
        </Button>
      {/if}

      <!-- Completed -->
      {#if status === 'completed'}
        <div class="rounded-lg border border-green-200 bg-green-50 p-4 dark:border-green-900 dark:bg-green-950">
          <p class="mb-2 font-medium text-green-800 dark:text-green-200">Agent completed successfully.</p>
          {#if outputFiles.length > 0}
            <div class="space-y-2">
              {#each outputFiles as filePath}
                <Button variant="default" class="w-full" onclick={() => downloadOutput(filePath)}>
                  Download {filePath.split('/').pop()}
                </Button>
              {/each}
            </div>
          {/if}
        </div>
      {/if}

      <!-- Terminated -->
      {#if status === 'terminated'}
        <div class="rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-900 dark:bg-amber-950">
          <p class="mb-2 font-medium text-amber-800 dark:text-amber-200">Agent could not process this file</p>
          {#if terminateReason}
            <p class="text-sm text-amber-700 dark:text-amber-300">{terminateReason}</p>
          {/if}
        </div>
      {/if}

      <!-- Failed -->
      {#if status === 'failed'}
        <div class="rounded-lg border border-destructive/20 bg-destructive/5 p-4">
          <p class="mb-2 font-medium text-destructive">Agent failed</p>
          {#if error}
            <pre class="overflow-x-auto rounded-md bg-neutral-900 p-3 text-xs text-red-400">{error}</pre>
          {/if}
        </div>
      {/if}

      <!-- Disconnected -->
      {#if status === 'disconnected'}
        <div class="rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-900 dark:bg-amber-950">
          <p class="mb-2 text-sm text-amber-800 dark:text-amber-200">WebSocket disconnected. The job may still be running.</p>
          <Button variant="outline" size="sm" onclick={() => window.location.reload()}>Reconnect</Button>
        </div>
      {/if}

      <Button variant="outline" class="w-full" onclick={() => push('/upload')}>
        Upload Another File
      </Button>
    </Card.Content>
  </Card.Root>
</div>

{#if askQuestion}
  <AskHumanDialog question={askQuestion} on:respond={handleHumanResponse} />
{/if}
