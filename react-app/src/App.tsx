/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import React, { useEffect } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuthStore } from './store/auth';
import { usePushNotifications } from './hooks/usePushNotifications';
import { AppShell } from './components/layout/AppShell';

// Auth pages
import Login from './pages/auth/Login';
import Register from './pages/auth/Register';

// Main pages
import Marketplace from './pages/market/Marketplace';
import ProductDetail from './pages/market/ProductDetail';
import NewRequest from './pages/market/NewRequest';
import Cart from './pages/orders/Cart';
import OrderList from './pages/orders/OrderList';
import FarmerStock from './pages/profile/FarmerStock';
import Profile from './pages/profile/Profile';
import AdminDashboard from './pages/admin/AdminDashboard';

function PrivateRoute({ children }: { children: React.ReactNode }) {
  const { user, initialized } = useAuthStore();
  if (!initialized) return <div className="min-h-screen bg-gray-950 flex items-center justify-center text-gray-400">Loading…</div>;
  if (!user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function PublicRoute({ children }: { children: React.ReactNode }) {
  const { user, initialized } = useAuthStore();
  if (!initialized) return <div className="min-h-screen bg-gray-950 flex items-center justify-center text-gray-400">Loading…</div>;
  if (user) return <Navigate to="/market" replace />;
  return <>{children}</>;
}

export default function App() {
  const { initialize, user } = useAuthStore();
  usePushNotifications(user?.id ?? null);

  useEffect(() => {
    initialize();
  }, []);

  return (
    <Routes>
      {/* Public auth routes */}
      <Route path="/login" element={<PublicRoute><Login /></PublicRoute>} />
      <Route path="/register" element={<PublicRoute><Register /></PublicRoute>} />

      {/* Protected app routes — wrapped in AppShell (header + bottom nav) */}
      <Route element={<PrivateRoute><AppShell /></PrivateRoute>}>
        <Route path="/market" element={<Marketplace />} />
        <Route path="/market/:id" element={<ProductDetail />} />
        <Route path="/requests/new" element={<NewRequest />} />
        <Route path="/cart" element={<Cart />} />
        <Route path="/orders" element={<OrderList />} />
        <Route path="/stock" element={<FarmerStock />} />
        <Route path="/profile" element={<Profile />} />
        <Route path="/admin" element={<AdminDashboard />} />
      </Route>

      {/* Catch-all → market */}
      <Route path="*" element={<Navigate to="/market" replace />} />
    </Routes>
  );
}
