/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { requestsApi } from '../../services/api';
import { Button } from '../../components/ui/Button';
import { Input } from '../../components/ui/Input';

export default function NewRequest() {
  const navigate = useNavigate();
  const [product, setProduct] = useState('');
  const [quantity, setQuantity] = useState('');
  const [unit, setUnit] = useState('kg');
  const [district, setDistrict] = useState('');
  const [address, setAddress] = useState('');
  const [targetPrice, setTargetPrice] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await requestsApi.create({
        product,
        quantity: parseFloat(quantity),
        unit,
        delivery_district: district || null,
        delivery_address: address,
        target_price: targetPrice ? parseFloat(targetPrice) : null,
      });
      navigate('/orders');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="px-4 py-6">
      <h2 className="text-xl font-bold text-white mb-6">Request Produce</h2>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <Input label="Product" value={product} onChange={e => setProduct(e.target.value)} placeholder="e.g. Maize, Tomatoes" required />
        <div className="flex gap-2">
          <div className="flex-1">
            <Input label="Quantity" type="number" value={quantity} onChange={e => setQuantity(e.target.value)} min="0.1" step="0.1" required />
          </div>
          <div className="w-24">
            <label className="text-sm font-medium text-gray-300 mb-1 block">Unit</label>
            <select
              value={unit}
              onChange={e => setUnit(e.target.value)}
              className="w-full bg-gray-800 border border-gray-600 rounded-lg px-2 py-2 text-white"
            >
              {['kg', 'tonne', 'bag', 'crate', 'litre'].map(u => <option key={u}>{u}</option>)}
            </select>
          </div>
        </div>
        <Input label="District" value={district} onChange={e => setDistrict(e.target.value)} placeholder="e.g. Kampala" />
        <Input label="Delivery Address" value={address} onChange={e => setAddress(e.target.value)} placeholder="Where should it be delivered?" />
        <Input label="Target Price (UGX/unit, optional)" type="number" value={targetPrice} onChange={e => setTargetPrice(e.target.value)} min="0" />
        {error && <p className="text-red-400 text-sm">{error}</p>}
        <Button type="submit" loading={loading} fullWidth>Submit Request</Button>
        <Button type="button" variant="ghost" onClick={() => navigate(-1)} fullWidth>Cancel</Button>
      </form>
    </div>
  );
}
