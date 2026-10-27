/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useCartStore } from '../../store/cart';
import { ordersApi } from '../../services/api';
import { Button } from '../../components/ui/Button';

export default function Cart() {
  const navigate = useNavigate();
  const { items, removeItem, updateQuantity, clear, total } = useCartStore();
  const [address, setAddress] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleCheckout = async () => {
    if (!address.trim()) { setError('Please enter a delivery address.'); return; }
    setError('');
    setLoading(true);
    try {
      // One order per item (mandate: route through /api/orders)
      for (const item of items) {
        await ordersApi.create({
          listing_id: item.listing.id,
          farmer_id: item.listing.farmer_id,
          product: item.listing.product,
          quantity: item.quantity,
          unit: item.listing.unit,
          price_per_unit: item.listing.price_per_unit,
          currency: item.listing.currency,
          delivery_address: address,
        });
      }
      clear();
      navigate('/orders');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  if (items.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-24 text-gray-400">
        <div className="text-5xl mb-4">🛍️</div>
        <p>Your cart is empty.</p>
        <Button variant="ghost" className="mt-4" onClick={() => navigate('/market')}>Browse Market</Button>
      </div>
    );
  }

  return (
    <div className="px-4 py-4 flex flex-col gap-4">
      <h2 className="text-xl font-bold text-white">Your Cart</h2>

      {items.map(item => (
        <div key={item.listing.id} className="bg-gray-800 rounded-xl p-4 flex flex-col gap-2">
          <div className="flex justify-between items-start">
            <div>
              <h3 className="font-semibold text-white">{item.listing.product}</h3>
              <p className="text-gray-400 text-sm">{item.listing.currency} {item.listing.price_per_unit.toLocaleString()} / {item.listing.unit}</p>
            </div>
            <button onClick={() => removeItem(item.listing.id)} className="text-red-400 text-sm">Remove</button>
          </div>
          <div className="flex items-center gap-3">
            <button onClick={() => updateQuantity(item.listing.id, item.quantity - 1)} className="bg-gray-700 text-white w-8 h-8 rounded-lg">−</button>
            <span className="text-white">{item.quantity}</span>
            <button onClick={() => updateQuantity(item.listing.id, item.quantity + 1)} className="bg-gray-700 text-white w-8 h-8 rounded-lg">+</button>
            <span className="text-green-400 ml-auto">= {item.listing.currency} {(item.listing.price_per_unit * item.quantity).toLocaleString()}</span>
          </div>
        </div>
      ))}

      <div className="bg-gray-800 rounded-xl p-4">
        <div className="flex justify-between text-white font-bold text-lg mb-4">
          <span>Total</span>
          <span>UGX {total().toLocaleString()}</span>
        </div>
        <label className="text-sm text-gray-300 mb-1 block">Delivery Address</label>
        <textarea
          value={address}
          onChange={e => setAddress(e.target.value)}
          rows={2}
          className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-green-500 mb-3"
          placeholder="Enter full delivery address"
        />
        {error && <p className="text-red-400 text-sm mb-2">{error}</p>}
        <Button onClick={handleCheckout} loading={loading} fullWidth>
          Place Order
        </Button>
      </div>
    </div>
  );
}
