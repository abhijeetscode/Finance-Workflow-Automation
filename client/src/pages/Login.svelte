<script>
  import { login } from '../stores/auth.js';
  import { push } from 'svelte-spa-router';
  import { Button } from '$lib/components/ui/button';
  import { Input } from '$lib/components/ui/input';
  import { Label } from '$lib/components/ui/label';
  import * as Card from '$lib/components/ui/card';

  let username = '';
  let password = '';
  let error = '';
  let loading = false;

  async function handleSubmit(e) {
    e.preventDefault();
    error = '';
    loading = true;
    try {
      console.log('[Login] calling login...');
      await login(username, password);
      console.log('[Login] login success, token in sessionStorage:', sessionStorage.getItem('token')?.slice(0, 20));
      console.log('[Login] pushing to /upload...');
      push('/upload');
      console.log('[Login] push called');
    } catch (e) {
      console.error('[Login] error:', e);
      error = e.message;
    } finally {
      loading = false;
    }
  }
</script>

<div class="flex min-h-screen items-center justify-center bg-background">
  <Card.Root class="w-full max-w-sm">
    <Card.Header>
      <Card.Title class="text-2xl">CodeGen Agent</Card.Title>
      <Card.Description>Sign in to continue</Card.Description>
    </Card.Header>
    <Card.Content>
      <form onsubmit={handleSubmit} class="space-y-4">
        <div class="space-y-2">
          <Label for="username">Username</Label>
          <Input
            id="username"
            type="text"
            bind:value={username}
            placeholder="Enter username"
            required
          />
        </div>

        <div class="space-y-2">
          <Label for="password">Password</Label>
          <Input
            id="password"
            type="password"
            bind:value={password}
            placeholder="Enter password"
            required
          />
        </div>

        {#if error}
          <div class="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        {/if}

        <Button type="submit" class="w-full" disabled={loading}>
          {loading ? 'Signing in...' : 'Sign In'}
        </Button>
      </form>
    </Card.Content>
  </Card.Root>
</div>
