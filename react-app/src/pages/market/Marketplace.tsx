/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { listingsApi } from '../../services/api';
import type { Listing } from '../../types';

function ListingCard({ listing }: { listing: Listing }) {
  return (
    <Link
      to={`/market/${listing.id}`}
      className="bg-gray-800 rounded-xl overflow-hidden border border-gray-700 hover:border-green-500 transition-colors"
    >
      {listing.images?.[0] ? (
        <img src={listing.images[0]} alt={listing.product} className="w-full h-36 object-cover" />
      ) : (
        <div className="w-full h-36 bg-gray-700 flex items-center justify-center text-4xl">🌾</div>
      )}
      <div className="p-3">
        <h3 className="font-semibold text-white truncate">{listing.product}</h3>
        <p className="text-green-400 text-sm mt-1">
          {listing.currency} {listing.price_per_unit.toLocaleString()} / {listing.unit}
        </p>
        <p className="text-gray-400 text-xs mt-1">
          {listing.quantity} {listing.unit} available
          {listing.district ? ` · ${listing.district}` : ''}
        </p>
      </div>
    </Link>
  );
}

export default function Marketplace() {
  const [listings, setListings] = useState<Listing[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const data = await listingsApi.list({ status: 'active' });
        setListings(data.listings ?? []);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const filtered = listings.filter(l =>
    l.product.toLowerCase().includes(search.toLowerCase()) ||
    (l.district || '').toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div className="px-4 py-4">
      {/* Search bar */}
      <input
        type="search"
        placeholder="Search produce, district…"
        value={search}
        onChange={e => setSearch(e.target.value)}
        className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-white placeholder-gray-500 mb-4 focus:outline-none focus:ring-2 focus:ring-green-500"
      />

      {loading && (
        <div className="text-center py-12 text-gray-400">Loading listings…</div>
      )}
      {error && (
        <div className="text-center py-12 text-red-400">{error}</div>
      )}
      {!loading && !error && filtered.length === 0 && (
        <div className="text-center py-12 text-gray-400">No listings found.</div>
      )}

      <div className="grid grid-cols-2 gap-3">
        {filtered.map(l => <ListingCard key={l.id} listing={l} />)}
      </div>

      {/* Request produce button */}
      <Link
        to="/requests/new"
        className="fixed bottom-24 right-4 bg-green-600 text-white rounded-full px-4 py-3 shadow-lg text-sm font-medium"
      >
        + Request Produce
      </Link>
    </div>
  );
}
