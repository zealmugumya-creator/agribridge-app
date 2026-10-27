/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

// ─── Shared TypeScript types for AgriBridge React app ───────────────────────

export type UserRole = 'buyer' | 'farmer' | 'admin';

export interface Profile {
  id: string;
  user_id: string;
  role: UserRole;
  full_name: string | null;
  phone: string | null;
  district: string | null;
  photo_url: string | null;
  bio: string | null;
  created_at: string;
}

export interface Listing {
  id: string;
  farmer_id: string;
  product: string;
  quantity: number;
  unit: string;
  price_per_unit: number;
  currency: string;
  district: string | null;
  description: string | null;
  images: string[];
  status: 'active' | 'reserved' | 'sold' | 'inactive';
  created_at: string;
  updated_at: string;
  // joined
  farmer?: Profile;
}

export interface PurchaseRequest {
  id: string;
  buyer_id: string;
  product: string;
  quantity: number;
  unit: string;
  delivery_address: string;
  delivery_district: string | null;
  target_price: number | null;
  status: 'pending' | 'matching' | 'offered' | 'accepted' | 'fulfilled' | 'cancelled';
  created_at: string;
  updated_at: string;
}

export interface OrderOffer {
  id: string;
  request_id: string;
  farmer_id: string;
  listing_id: string | null;
  offered_quantity: number;
  unit: string;
  price_per_unit: number;
  status: 'pending' | 'accepted' | 'rejected' | 'expired';
  expires_at: string;
  created_at: string;
  // joined
  farmer?: Profile;
  request?: PurchaseRequest;
}

export interface Order {
  id: string;
  buyer_id: string;
  farmer_id: string;
  listing_id: string | null;
  offer_id: string | null;
  product: string;
  quantity: number;
  unit: string;
  price_per_unit: number;
  total_amount: number;
  currency: string;
  status: 'pending' | 'confirmed' | 'processing' | 'shipped' | 'delivered' | 'cancelled';
  payment_status: 'unpaid' | 'paid' | 'refunded';
  delivery_address: string;
  notes: string | null;
  created_at: string;
  updated_at: string;
  // joined
  buyer?: Profile;
  farmer?: Profile;
}

export interface CartItem {
  listing: Listing;
  quantity: number;
}

export interface ApiError {
  message: string;
  status?: number;
}
