/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuthStore } from '../../store/auth';
import { Button } from '../../components/ui/Button';
import { Input } from '../../components/ui/Input';
import type { UserRole } from '../../types';

export default function Register() {
  const navigate = useNavigate();
  const { signUp, loading } = useAuthStore();
  const [role, setRole] = useState<UserRole>('buyer');
  const [fullName, setFullName] = useState('');
  const [phone, setPhone] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    const { error: err } = await signUp(email, password, role, fullName, phone);
    if (err) {
      setError(err);
    } else {
      navigate('/market');
    }
  };

  return (
    <div className="min-h-screen bg-gray-950 flex flex-col items-center justify-center px-6 py-8">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="text-5xl mb-3">🌿</div>
          <h1 className="text-2xl font-bold text-green-400">Create Account</h1>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          {/* Role selector */}
          <div>
            <p className="text-sm font-medium text-gray-300 mb-2">I am a…</p>
            <div className="flex gap-2">
              {(['buyer', 'farmer'] as UserRole[]).map(r => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setRole(r)}
                  className={`flex-1 py-2 rounded-lg border text-sm font-medium capitalize transition-colors
                    ${role === r
                      ? 'bg-green-600 border-green-500 text-white'
                      : 'bg-gray-800 border-gray-600 text-gray-300'}`}
                >
                  {r === 'buyer' ? '🛒 Buyer' : '🌾 Farmer'}
                </button>
              ))}
            </div>
          </div>

          <Input label="Full Name" value={fullName} onChange={e => setFullName(e.target.value)} required />
          <Input label="Phone" type="tel" value={phone} onChange={e => setPhone(e.target.value)} placeholder="+256..." />
          <Input label="Email" type="email" value={email} onChange={e => setEmail(e.target.value)} required />
          <Input label="Password" type="password" value={password} onChange={e => setPassword(e.target.value)} minLength={8} required />

          {error && <p className="text-red-400 text-sm text-center">{error}</p>}
          <Button type="submit" loading={loading} fullWidth>
            Create Account
          </Button>
        </form>

        <p className="text-center text-gray-400 text-sm mt-6">
          Already have an account?{' '}
          <Link to="/login" className="text-green-400 underline">Sign in</Link>
        </p>
      </div>
    </div>
  );
}
