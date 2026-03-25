<script>
  import { token } from '../stores/auth.js';
  import { API_BASE } from '../stores/api.js';
  import { push } from 'svelte-spa-router';
  import { Button } from '$lib/components/ui/button';
  import * as Card from '$lib/components/ui/card';

  let file = null;
  let error = '';
  let loading = false;
  let dragOver = false;

  function handleFileSelect(e) {
    const selected = e.target.files[0];
    if (selected && selected.name.endsWith('.xlsx')) {
      file = selected;
      error = '';
    } else {
      error = 'Please select an .xlsx file';
    }
  }

  function handleDrop(e) {
    dragOver = false;
    const dropped = e.dataTransfer.files[0];
    if (dropped && dropped.name.endsWith('.xlsx')) {
      file = dropped;
      error = '';
    } else {
      error = 'Please drop an .xlsx file';
    }
  }

  async function handleUpload() {
    if (!file) return;
    error = '';
    loading = true;

    try {
      const formData = new FormData();
      formData.append('file', file);

      const res = await fetch(`${API_BASE}/agent/run`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${$token}` },
        body: formData,
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || 'Upload failed');
      }

      const data = await res.json();
      push(`/job/${data.job_id}`);
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }
</script>

<div class="flex justify-center pt-16">
  <Card.Root class="w-full max-w-lg">
    <Card.Header>
      <Card.Title>Upload Excel File</Card.Title>
      <Card.Description>Upload a vendor payment file (.xlsx) for processing</Card.Description>
    </Card.Header>
    <Card.Content class="space-y-4">
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        role="button"
        tabindex="0"
        class="rounded-lg border-2 border-dashed p-8 text-center transition-colors
          {dragOver ? 'border-primary bg-primary/5' : file ? 'border-green-500 bg-green-500/5' : 'border-border'}"
        ondragover={(e) => { e.preventDefault(); dragOver = true; }}
        ondragleave={() => (dragOver = false)}
        ondrop={(e) => { e.preventDefault(); handleDrop(e); }}
      >
        {#if file}
          <div class="flex items-center justify-center gap-2">
            <span class="text-lg">&#128196;</span>
            <span class="font-medium">{file.name}</span>
            <span class="text-sm text-muted-foreground">({(file.size / 1024).toFixed(1)} KB)</span>
            <button
              class="ml-1 text-muted-foreground hover:text-destructive"
              onclick={(e) => { e.stopPropagation(); file = null; }}
            >&#10005;</button>
          </div>
        {:else}
          <p class="mb-2 text-muted-foreground">Drag & drop your .xlsx file here, or</p>
          <label class="inline-block cursor-pointer rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90">
            Browse
            <input type="file" accept=".xlsx" onchange={handleFileSelect} hidden />
          </label>
        {/if}
      </div>

      {#if error}
        <div class="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      {/if}

      <Button class="w-full" onclick={handleUpload} disabled={!file || loading}>
        {loading ? 'Uploading...' : 'Process File'}
      </Button>
    </Card.Content>
  </Card.Root>
</div>
