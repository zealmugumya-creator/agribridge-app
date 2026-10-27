/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import { useAuthStore } from '../../store/auth';
import { useNavigate } from 'react-router-dom';

/**
 * Admin Dashboard — thin wrapper that links to the existing Flask admin UI.
 * Full admin functionality lives at /admin.html served by the Flask backend.
 * This page is accessible only to users whose Supabase profile has role=admin.
 */
export default function AdminDashboard() {
  const { profile } = useAuthStore();
  const navigate = useNavigate();

  if (profile?.role !== 'admin') {
    navigate('/market');
    return null;
  }

  const backendAdminUrl = `${import.meta.env.VITE_API_URL || 'https://agribridge-1-og7a.onrender.com'}/admin.html`;

  return (
    <div className="px-4 py-6 flex flex-col gap-6">
      <h2 className="text-xl font-bold text-white">Admin Centre</h2>

      <div className="bg-gray-800 rounded-xl p-4 flex flex-col gap-4">
        <p className="text-gray-300 text-sm">
          The full admin command centre (users, listings, orders, purchase requests, offer management, and the matching engine) is available in the web admin panel.
        </p>
        <a
          href={backendAdminUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="bg-green-600 hover:bg-green-700 text-white text-center py-3 rounded-lg font-medium transition-colors"
        >
          Open Admin Panel ↗
        </a>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {[
          { emoji: '👥', label: 'Users', path: '/admin/users' },
          { emoji: '🌾', label: 'Listings', path: '/admin/listings' },
          { emoji: '📦', label: 'Orders', path: '/admin/orders' },
          { emoji: '🤝', label: 'Matching', path: '/admin/matching' },
        ].map(({ emoji, label, path }) => (
          <button
            key={path}
            onClick={() => window.open(backendAdminUrl, '_blank')}
            className="bg-gray-800 rounded-xl p-4 flex flex-col items-center gap-2 border border-gray-700 hover:border-green-500 transition-colors"
          >
            <span className="text-2xl">{emoji}</span>
            <span className="text-sm text-gray-300">{label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
