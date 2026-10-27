/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import { Outlet } from 'react-router-dom';
import { BottomNav } from './BottomNav';

export function AppShell() {
  return (
    <div className="min-h-screen bg-gray-950 text-white flex flex-col">
      {/* Header */}
      <header className="bg-gray-900 border-b border-gray-700 px-4 py-3 safe-area-top">
        <h1 className="text-lg font-bold text-green-400">🌿 AgriBridge</h1>
      </header>

      {/* Page content — padded so it doesn't hide behind the bottom nav */}
      <main className="flex-1 overflow-y-auto pb-20">
        <Outlet />
      </main>

      <BottomNav />
    </div>
  );
}
