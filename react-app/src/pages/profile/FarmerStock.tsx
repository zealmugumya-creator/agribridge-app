/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import React, { useEffect, useState } from 'react';
import { useAuthStore } from '../../store/auth';
import { listingsApi } from '../../services/api';
import { Button } from '../../components/ui/Button';
import { Input } from '../../components/ui/Input';
import type { Listing } from '../../types';

function ListingRow({ listing, onToggle, onDelete }: {
  listing: Listing;
  onToggle: () => void;
  onDelete: () => void;
}) {
  return (
    <div className="bg-gray-800 rounded-xl p-4 border border-gray-700 flex flex-col gap-2">
      <div className="flex justify-between items-start">
        <div>
          <h3 className="font-semibold text-white">{listing.product}</h3>
          <p className="text-gray-400 text-sm">
            {listing.quantity} {listing.unit} · UGX {listing.price_per_unit.toLocaleString()}/{listing.unit}
          </p>
        </div>
        <span className={`text-xs px-2 py-1 rounded-full ${
          listing.status === 'active' ? 'bg-green-900 text-green-300' : 'bg-gray-700 text-gray-400'
        }`}>
          {listing.status}
        </span>
      </div>
      <div className="flex gap-2 mt-1">
        <Button size="sm" variant="secondary" onClick={onToggle}>
          {listing.status === 'active' ? 'Deactivate' : 'Activate'}
        </Button>
        <Button size="sm" variant="danger" onClick={onDelete}>Delete</Button>
      </div>
    </div>
  );
}

const EMPTY_FORM = { product: '', quantity: '', unit: 'kg', price_per_unit: '', district: '', description: '' };

export default function FarmerStock() {
  const { user } = useAuthStore();
  const [listings, setListings] = useState<Listing[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const fetchListings = async () => {
    if (!user) return;
    setLoading(true);
    try {
      const data = await listingsApi.list({ farmer_id: user.id });
      setListings(data.listings ?? []);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchListings(); }, [user]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSubmitting(true);
    try {
      await listingsApi.create({
        product: form.product,
        quantity: parseFloat(form.quantity),
        unit: form.unit,
        price_per_unit: parseFloat(form.price_per_unit),
        district: form.district || null,
        description: form.description || null,
      });
      setForm(EMPTY_FORM);
      setShowForm(false);
      await fetchListings();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleToggle = async (listing: Listing) => {
    const newStatus = listing.status === 'active' ? 'inactive' : 'active';
    await listingsApi.update(listing.id, { status: newStatus });
    await fetchListings();
  };

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this listing?')) return;
    await listingsApi.delete(id);
    await fetchListings();
  };

  return (
    <div className="px-4 py-4">
      <div className="flex justify-between items-center mb-4">
        <h2 className="text-xl font-bold text-white">My Stock</h2>
        <Button size="sm" onClick={() => setShowForm(v => !v)}>
          {showForm ? 'Cancel' : '+ Add Listing'}
        </Button>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className="bg-gray-800 rounded-xl p-4 mb-4 flex flex-col gap-3">
          <Input label="Product" value={form.product} onChange={e => setForm(f => ({ ...f, product: e.target.value }))} required />
          <div className="flex gap-2">
            <div className="flex-1">
              <Input label="Quantity" type="number" value={form.quantity} onChange={e => setForm(f => ({ ...f, quantity: e.target.value }))} min="0.1" step="0.1" required />
            </div>
            <div className="w-24">
              <label className="text-sm font-medium text-gray-300 mb-1 block">Unit</label>
              <select value={form.unit} onChange={e => setForm(f => ({ ...f, unit: e.target.value }))}
                className="w-full bg-gray-700 border border-gray-600 rounded-lg px-2 py-2 text-white">
                {['kg', 'tonne', 'bag', 'crate', 'litre'].map(u => <option key={u}>{u}</option>)}
              </select>
            </div>
          </div>
          <Input label="Price / unit (UGX)" type="number" value={form.price_per_unit} onChange={e => setForm(f => ({ ...f, price_per_unit: e.target.value }))} min="0" required />
          <Input label="District (optional)" value={form.district} onChange={e => setForm(f => ({ ...f, district: e.target.value }))} />
          <Input label="Description (optional)" value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))} />
          {error && <p className="text-red-400 text-sm">{error}</p>}
          <Button type="submit" loading={submitting} fullWidth>Save Listing</Button>
        </form>
      )}

      {loading ? (
        <div className="text-center py-12 text-gray-400">Loading…</div>
      ) : listings.length === 0 ? (
        <div className="text-center py-12 text-gray-400">No listings yet. Add your first stock item.</div>
      ) : (
        <div className="flex flex-col gap-3">
          {listings.map(l => (
            <ListingRow key={l.id} listing={l} onToggle={() => handleToggle(l)} onDelete={() => handleDelete(l.id)} />
          ))}
        </div>
      )}
    </div>
  );
}
