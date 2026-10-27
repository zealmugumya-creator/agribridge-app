/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { listingsApi } from '../../services/api';
import { useCartStore } from '../../store/cart';
import { useAuthStore } from '../../store/auth';
import { Button } from '../../components/ui/Button';
import type { Listing } from '../../types';

export default function ProductDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { profile } = useAuthStore();
  const addItem = useCartStore(s => s.addItem);
  const [listing, setListing] = useState<Listing | null>(null);
  const [qty, setQty] = useState(1);
  const [loading, setLoading] = useState(true);
  const [added, setAdded] = useState(false);

  useEffect(() => {
    if (!id) return;
    listingsApi.get(id).then(l => { setListing(l); setLoading(false); }).catch(() => setLoading(false));
  }, [id]);

  const handleAddToCart = () => {
    if (!listing) return;
    addItem(listing, qty);
    setAdded(true);
    setTimeout(() => setAdded(false), 2000);
  };

  if (loading) return <div className="p-6 text-gray-400">Loading…</div>;
  if (!listing) return <div className="p-6 text-red-400">Listing not found.</div>;

  return (
    <div className="flex flex-col">
      {/* Image */}
      {listing.images?.[0] ? (
        <img src={listing.images[0]} alt={listing.product} className="w-full h-56 object-cover" />
      ) : (
        <div className="w-full h-56 bg-gray-700 flex items-center justify-center text-6xl">🌾</div>
      )}

      <div className="px-4 py-4 flex flex-col gap-4">
        <div>
          <h1 className="text-xl font-bold text-white">{listing.product}</h1>
          {listing.district && <p className="text-gray-400 text-sm mt-1">📍 {listing.district}</p>}
        </div>

        <div className="bg-gray-800 rounded-xl p-4 flex flex-col gap-2">
          <div className="flex justify-between">
            <span className="text-gray-400">Price</span>
            <span className="text-green-400 font-semibold">
              {listing.currency} {listing.price_per_unit.toLocaleString()} / {listing.unit}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-400">Available</span>
            <span className="text-white">{listing.quantity} {listing.unit}</span>
          </div>
          {listing.farmer && (
            <div className="flex justify-between">
              <span className="text-gray-400">Farmer</span>
              <span className="text-white">{listing.farmer.full_name}</span>
            </div>
          )}
        </div>

        {listing.description && (
          <p className="text-gray-300 text-sm">{listing.description}</p>
        )}

        {/* Quantity + Add to cart */}
        {profile?.role === 'buyer' && listing.status === 'active' && (
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-4">
              <span className="text-gray-300 text-sm">Quantity ({listing.unit})</span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setQty(q => Math.max(1, q - 1))}
                  className="bg-gray-700 text-white w-8 h-8 rounded-lg text-lg"
                >−</button>
                <span className="text-white w-8 text-center">{qty}</span>
                <button
                  onClick={() => setQty(q => Math.min(listing.quantity, q + 1))}
                  className="bg-gray-700 text-white w-8 h-8 rounded-lg text-lg"
                >+</button>
              </div>
            </div>
            <p className="text-gray-400 text-sm">
              Total: {listing.currency} {(listing.price_per_unit * qty).toLocaleString()}
            </p>
            <Button onClick={handleAddToCart} fullWidth>
              {added ? '✓ Added to Cart' : 'Add to Cart'}
            </Button>
          </div>
        )}

        <Button variant="ghost" onClick={() => navigate(-1)} size="sm">
          ← Back to Market
        </Button>
      </div>
    </div>
  );
}
