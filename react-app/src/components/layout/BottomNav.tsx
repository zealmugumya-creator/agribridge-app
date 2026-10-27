/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import React from 'react';
import { NavLink } from 'react-router-dom';
import { useAuthStore } from '../../store/auth';
import { useCartStore } from '../../store/cart';

const NavItem = ({ to, icon, label, badge }: { to: string; icon: React.ReactNode; label: string; badge?: number }) => (
  <NavLink
    to={to}
    className={({ isActive }) =>
      `flex flex-col items-center gap-0.5 px-3 py-2 text-xs relative ${
        isActive ? 'text-green-400' : 'text-gray-400'
      }`
    }
  >
    <span className="text-xl relative">
      {icon}
      {badge != null && badge > 0 && (
        <span className="absolute -top-1 -right-1 bg-red-500 text-white text-[10px] rounded-full w-4 h-4 flex items-center justify-center">
          {badge > 9 ? '9+' : badge}
        </span>
      )}
    </span>
    {label}
  </NavLink>
);

export function BottomNav() {
  const { profile } = useAuthStore();
  const cartCount = useCartStore(s => s.items.length);

  return (
    <nav className="fixed bottom-0 left-0 right-0 bg-gray-900 border-t border-gray-700 flex justify-around safe-area-bottom z-50">
      <NavItem to="/market" icon="🛒" label="Market" />
      <NavItem to="/orders" icon="📦" label="Orders" />
      {profile?.role === 'farmer' && <NavItem to="/stock" icon="🌾" label="Stock" />}
      {profile?.role === 'buyer' && <NavItem to="/cart" icon="🛍️" label="Cart" badge={cartCount} />}
      {profile?.role === 'admin' && <NavItem to="/admin" icon="⚙️" label="Admin" />}
      <NavItem to="/profile" icon="👤" label="Profile" />
    </nav>
  );
}
