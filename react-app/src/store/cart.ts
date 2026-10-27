/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import { create } from 'zustand';
import type { CartItem, Listing } from '../types';

interface CartState {
  items: CartItem[];
  addItem: (listing: Listing, quantity: number) => void;
  removeItem: (listingId: string) => void;
  updateQuantity: (listingId: string, quantity: number) => void;
  clear: () => void;
  total: () => number;
}

export const useCartStore = create<CartState>((set, get) => ({
  items: [],

  addItem: (listing, quantity) => {
    set((state) => {
      const existing = state.items.find(i => i.listing.id === listing.id);
      if (existing) {
        return {
          items: state.items.map(i =>
            i.listing.id === listing.id
              ? { ...i, quantity: i.quantity + quantity }
              : i
          ),
        };
      }
      return { items: [...state.items, { listing, quantity }] };
    });
  },

  removeItem: (listingId) => {
    set((state) => ({ items: state.items.filter(i => i.listing.id !== listingId) }));
  },

  updateQuantity: (listingId, quantity) => {
    if (quantity <= 0) {
      get().removeItem(listingId);
      return;
    }
    set((state) => ({
      items: state.items.map(i =>
        i.listing.id === listingId ? { ...i, quantity } : i
      ),
    }));
  },

  clear: () => set({ items: [] }),

  total: () => {
    return get().items.reduce(
      (sum, item) => sum + item.listing.price_per_unit * item.quantity,
      0
    );
  },
}));
