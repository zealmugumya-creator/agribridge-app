/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuthStore } from '../../store/auth';
import { Button } from '../../components/ui/Button';
import { Input } from '../../components/ui/Input';

export default function Login() {
  const navigate = useNavigate();
  const { signIn, loading } = useAuthStore();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    const { error: err } = await signIn(email, password);
    if (err) {
      setError(err);
    } else {
      navigate('/market');
    }
  };

  return (
    <div className="min-h-screen bg-gray-950 flex flex-col items-center justify-center px-6">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="text-5xl mb-3">🌿</div>
          <h1 className="text-2xl font-bold text-green-400">AgriBridge</h1>
          <p className="text-gray-400 text-sm mt-1">Farm-to-market marketplace</p>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <Input
            label="Email"
            type="email"
            value={email}
            onChange={e => setEmail(e.target.value)}
            autoComplete="email"
            required
          />
          <Input
            label="Password"
            type="password"
            value={password}
            onChange={e => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
          {error && <p className="text-red-400 text-sm text-center">{error}</p>}
          <Button type="submit" loading={loading} fullWidth>
            Sign In
          </Button>
        </form>

        <p className="text-center text-gray-400 text-sm mt-6">
          New to AgriBridge?{' '}
          <Link to="/register" className="text-green-400 underline">
            Create account
          </Link>
        </p>
      </div>
    </div>
  );
}
