/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

/**
 * AgriBridge API service — all calls go to the Flask backend.
 * Auth token is read from Supabase session; never uses the service-role key.
 */
import { supabase } from '../lib/supabase';

const BASE = import.meta.env.VITE_API_URL as string || 'https://agribridge-1-og7a.onrender.com';

async function getToken(): Promise<string | null> {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = await getToken();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(`${BASE}${path}`, { ...options, headers });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error((json as { error?: string }).error || `HTTP ${res.status}`);
  }
  return json as T;
}

// ── Market listings ──────────────────────────────────────────────────────────
export const listingsApi = {
  list: (params?: Record<string, string>) => {
    const qs = params ? '?' + new URLSearchParams(params).toString() : '';
    return request<{ listings: import('../types').Listing[] }>(`/api/listings${qs}`);
  },
  get: (id: string) => request<import('../types').Listing>(`/api/listings/${id}`),
  create: (body: object) => request<import('../types').Listing>('/api/listings', { method: 'POST', body: JSON.stringify(body) }),
  update: (id: string, body: object) => request<import('../types').Listing>(`/api/listings/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: (id: string) => request<{ ok: boolean }>(`/api/listings/${id}`, { method: 'DELETE' }),
};

// ── Purchase requests ────────────────────────────────────────────────────────
export const requestsApi = {
  create: (body: object) =>
    request<import('../types').PurchaseRequest>('/api/requests', { method: 'POST', body: JSON.stringify(body) }),
  myRequests: () =>
    request<import('../types').PurchaseRequest[]>('/api/requests/mine'),
};

// ── Offers ───────────────────────────────────────────────────────────────────
export const offersApi = {
  myOffers: () => request<import('../types').OrderOffer[]>('/api/offers/mine'),
  accept: (offerId: string) =>
    request<{ ok: boolean }>(`/api/offers/${offerId}/accept`, { method: 'POST', body: JSON.stringify({}) }),
  reject: (offerId: string) =>
    request<{ ok: boolean }>(`/api/offers/${offerId}/reject`, { method: 'POST', body: JSON.stringify({}) }),
};

// ── Orders ───────────────────────────────────────────────────────────────────
export const ordersApi = {
  create: (body: object) =>
    request<{ order_id: string; checkout_url?: string }>('/api/orders', { method: 'POST', body: JSON.stringify(body) }),
  myOrders: () => request<import('../types').Order[]>('/api/orders/mine'),
  get: (id: string) => request<import('../types').Order>(`/api/orders/${id}`),
};

// ── Profile ──────────────────────────────────────────────────────────────────
export const profileApi = {
  update: (body: object) =>
    request<import('../types').Profile>('/api/profile', { method: 'PATCH', body: JSON.stringify(body) }),
};
