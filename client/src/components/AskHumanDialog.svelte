<script>
  import { createEventDispatcher } from 'svelte';
  import { Button } from '$lib/components/ui/button';
  import { Input } from '$lib/components/ui/input';
  import { Badge } from '$lib/components/ui/badge';
  import * as Dialog from '$lib/components/ui/dialog';

  export let question = '';

  const dispatch = createEventDispatcher();
  let answer = '';

  function handleSubmit(e) {
    e.preventDefault();
    dispatch('respond', { answer });
    answer = '';
  }
</script>

<Dialog.Root open={true}>
  <Dialog.Content class="sm:max-w-md">
    <Dialog.Header>
      <Dialog.Title class="flex items-center gap-2">
        <Badge variant="outline" class="border-amber-500 text-amber-600">Human Input</Badge>
        Agent needs your help
      </Dialog.Title>
      <Dialog.Description>{question}</Dialog.Description>
    </Dialog.Header>
    <form onsubmit={handleSubmit} class="space-y-4">
      <textarea
        bind:value={answer}
        placeholder="Type your response..."
        rows="3"
        class="flex min-h-[80px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      ></textarea>
      <Dialog.Footer>
        <Button type="submit" disabled={!answer.trim()} class="w-full">
          Send Response
        </Button>
      </Dialog.Footer>
    </form>
  </Dialog.Content>
</Dialog.Root>
