/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import { useEffect, useState } from 'react';
import { useAuthStore } from '../../store/auth';
import { ordersApi, offersApi, requestsApi } from '../../services/api';
import { Button } from '../../components/ui/Button';
import type { Order, OrderOffer, PurchaseRequest } from '../../types';

const STATUS_COLORS: Record<string, string> = {
  pending: 'text-yellow-400',
  confirmed: 'text-blue-400',
  processing: 'text-blue-300',
  shipped: 'text-purple-400',
  delivered: 'text-green-400',
  cancelled: 'text-red-400',
  accepted: 'text-green-400',
  rejected: 'text-red-400',
  expired: 'text-gray-400',
  offered: 'text-yellow-400',
  fulfilled: 'text-green-400',
};

function OrderCard({ order }: { order: Order }) {
  return (
    <div className="bg-gray-800 rounded-xl p-4 border border-gray-700">
      <div className="flex justify-between items-start mb-2">
        <h3 className="font-semibold text-white">{order.product}</h3>
        <span className={`text-sm font-medium capitalize ${STATUS_COLORS[order.status] || 'text-gray-400'}`}>
          {order.status}
        </span>
      </div>
      <p className="text-gray-400 text-sm">
        {order.quantity} {order.unit} · UGX {order.total_amount?.toLocaleString()}
      </p>
      <p className="text-gray-500 text-xs mt-1">
        {new Date(order.created_at).toLocaleDateString()}
      </p>
    </div>
  );
}

function OfferCard({ offer, onAccept, onReject }: { offer: OrderOffer; onAccept: () => void; onReject: () => void }) {
  return (
    <div className="bg-gray-800 rounded-xl p-4 border border-gray-700">
      <div className="flex justify-between items-start mb-2">
        <h3 className="font-semibold text-white">Offer received</h3>
        <span className={`text-sm font-medium capitalize ${STATUS_COLORS[offer.status] || 'text-gray-400'}`}>
          {offer.status}
        </span>
      </div>
      <p className="text-gray-400 text-sm">
        {offer.offered_quantity} {offer.unit} · UGX {offer.price_per_unit?.toLocaleString()} / {offer.unit}
      </p>
      <p className="text-gray-400 text-sm">
        Farmer: {offer.farmer?.full_name || offer.farmer_id.slice(0, 8)}
      </p>
      {offer.status === 'pending' && (
        <div className="flex gap-2 mt-3">
          <Button size="sm" onClick={onAccept}>Accept</Button>
          <Button size="sm" variant="danger" onClick={onReject}>Reject</Button>
        </div>
      )}
    </div>
  );
}

function RequestCard({ req }: { req: PurchaseRequest }) {
  return (
    <div className="bg-gray-800 rounded-xl p-4 border border-gray-700">
      <div className="flex justify-between items-start mb-2">
        <h3 className="font-semibold text-white">{req.product}</h3>
        <span className={`text-sm font-medium capitalize ${STATUS_COLORS[req.status] || 'text-gray-400'}`}>
          {req.status}
        </span>
      </div>
      <p className="text-gray-400 text-sm">{req.quantity} {req.unit} requested</p>
      {req.delivery_district && <p className="text-gray-500 text-xs mt-1">📍 {req.delivery_district}</p>}
    </div>
  );
}

export default function OrderList() {
  const { profile } = useAuthStore();
  const [orders, setOrders] = useState<Order[]>([]);
  const [offers, setOffers] = useState<OrderOffer[]>([]);
  const [requests, setRequests] = useState<PurchaseRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<'orders' | 'offers' | 'requests'>('orders');

  const fetchAll = async () => {
    setLoading(true);
    try {
      const [ord, off, req] = await Promise.allSettled([
        ordersApi.myOrders(),
        offersApi.myOffers(),
        requestsApi.myRequests(),
      ]);
      if (ord.status === 'fulfilled') setOrders(ord.value);
      if (off.status === 'fulfilled') setOffers(off.value);
      if (req.status === 'fulfilled') setRequests(req.value);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchAll(); }, []);

  const handleOfferAction = async (offerId: string, action: 'accept' | 'reject') => {
    try {
      if (action === 'accept') await offersApi.accept(offerId);
      else await offersApi.reject(offerId);
      await fetchAll();
    } catch (e) {
      alert((e as Error).message);
    }
  };

  const tabs = [
    { key: 'orders' as const, label: 'Orders', count: orders.length },
    ...(profile?.role === 'farmer' ? [{ key: 'offers' as const, label: 'Offers', count: offers.filter(o => o.status === 'pending').length }] : []),
    ...(profile?.role === 'buyer' ? [{ key: 'requests' as const, label: 'Requests', count: requests.length }] : []),
  ];

  return (
    <div className="px-4 py-4">
      {/* Tabs */}
      <div className="flex gap-1 mb-4 bg-gray-800 rounded-lg p-1">
        {tabs.map(t => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex-1 py-1.5 text-sm rounded-md transition-colors ${
              tab === t.key ? 'bg-green-600 text-white' : 'text-gray-400'
            }`}
          >
            {t.label}{t.count > 0 ? ` (${t.count})` : ''}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="text-center py-12 text-gray-400">Loading…</div>
      ) : (
        <div className="flex flex-col gap-3">
          {tab === 'orders' && (orders.length === 0
            ? <p className="text-gray-400 text-center py-12">No orders yet.</p>
            : orders.map(o => <OrderCard key={o.id} order={o} />)
          )}
          {tab === 'offers' && (offers.length === 0
            ? <p className="text-gray-400 text-center py-12">No offers yet.</p>
            : offers.map(o => (
              <OfferCard
                key={o.id}
                offer={o}
                onAccept={() => handleOfferAction(o.id, 'accept')}
                onReject={() => handleOfferAction(o.id, 'reject')}
              />
            ))
          )}
          {tab === 'requests' && (requests.length === 0
            ? <p className="text-gray-400 text-center py-12">No requests yet.</p>
            : requests.map(r => <RequestCard key={r.id} req={r} />)
          )}
        </div>
      )}
    </div>
  );
}
