<script>
  import Router, { push } from 'svelte-spa-router';
  import { isAuthenticated, token, logout } from './stores/auth.js';
  import { get } from 'svelte/store';
  import Login from './pages/Login.svelte';
  import Upload from './pages/Upload.svelte';
  import JobStatus from './pages/JobStatus.svelte';
  import { wrap } from 'svelte-spa-router/wrap';
  import { Button } from '$lib/components/ui/button';

  function isAuthed() {
    const val = !!get(token);
    console.log('[Router] isAuthed check:', val, 'token:', get(token)?.slice(0, 20));
    return val;
  }

  const routes = {
    '/': Login,
    '/upload': wrap({
      component: Upload,
      conditions: [isAuthed],
    }),
    '/job/:id': wrap({
      component: JobStatus,
      conditions: [isAuthed],
    }),
  };

  function conditionsFailed(event) {
    console.log('[Router] conditionsFailed, redirecting to /. Event:', event?.detail);
    push('/');
  }
</script>

<div class="min-h-screen bg-background">
  {#if $isAuthenticated}
    <nav class="flex items-center justify-between border-b border-border px-6 py-3">
      <span class="text-sm font-semibold tracking-tight">CodeGen Agent</span>
      <div class="flex items-center gap-2">
        <Button variant="ghost" size="sm" onclick={() => push('/upload')}>Upload</Button>
        <Button variant="ghost" size="sm" class="text-destructive hover:text-destructive" onclick={logout}>Logout</Button>
      </div>
    </nav>
  {/if}

  <main>
    <Router {routes} on:conditionsFailed={conditionsFailed} />
  </main>
</div>
