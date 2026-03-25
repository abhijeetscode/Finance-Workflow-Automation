import { writable, derived } from 'svelte/store';
import { API_BASE } from './api.js';

const storedToken = sessionStorage.getItem('token');
export const token = writable(storedToken || null);
export const isAuthenticated = derived(token, ($token) => !!$token);

token.subscribe((value) => {
  if (value) {
    sessionStorage.setItem('token', value);
  } else {
    sessionStorage.removeItem('token');
  }
});

export async function login(username, password) {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });

  if (!res.ok) {
    const data = await res.json();
    throw new Error(data.detail || 'Login failed');
  }

  const data = await res.json();
  token.set(data.access_token);
}

export function logout() {
  token.set(null);
}
