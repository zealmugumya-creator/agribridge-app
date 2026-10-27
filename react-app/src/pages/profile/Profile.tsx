/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import React, { useState } from 'react';
import { useAuthStore } from '../../store/auth';
import { profileApi } from '../../services/api';
import { Button } from '../../components/ui/Button';
import { Input } from '../../components/ui/Input';

export default function Profile() {
  const { profile, signOut, refreshProfile } = useAuthStore();
  const [editing, setEditing] = useState(false);
  const [fullName, setFullName] = useState(profile?.full_name || '');
  const [phone, setPhone] = useState(profile?.phone || '');
  const [district, setDistrict] = useState(profile?.district || '');
  const [bio, setBio] = useState(profile?.bio || '');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState(false);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await profileApi.update({ full_name: fullName, phone, district, bio });
      await refreshProfile();
      setEditing(false);
      setSuccess(true);
      setTimeout(() => setSuccess(false), 3000);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const handleSignOut = async () => {
    if (!confirm('Sign out?')) return;
    await signOut();
  };

  if (!profile) return <div className="p-6 text-gray-400">Loading profile…</div>;

  return (
    <div className="px-4 py-6 flex flex-col gap-6">
      {/* Avatar + name */}
      <div className="flex items-center gap-4">
        <div className="w-16 h-16 rounded-full bg-green-700 flex items-center justify-center text-2xl font-bold text-white">
          {profile.full_name?.[0]?.toUpperCase() || '?'}
        </div>
        <div>
          <h2 className="text-xl font-bold text-white">{profile.full_name || 'No name set'}</h2>
          <span className="text-xs bg-green-900 text-green-300 px-2 py-0.5 rounded-full capitalize">{profile.role}</span>
        </div>
      </div>

      {success && <p className="text-green-400 text-sm text-center">Profile updated!</p>}

      {!editing ? (
        <div className="bg-gray-800 rounded-xl p-4 flex flex-col gap-3">
          {[
            { label: 'Phone', value: profile.phone },
            { label: 'District', value: profile.district },
            { label: 'Bio', value: profile.bio },
          ].map(({ label, value }) => (
            value && (
              <div key={label}>
                <p className="text-xs text-gray-500">{label}</p>
                <p className="text-white text-sm">{value}</p>
              </div>
            )
          ))}
          <Button onClick={() => setEditing(true)} variant="secondary">Edit Profile</Button>
        </div>
      ) : (
        <form onSubmit={handleSave} className="flex flex-col gap-3">
          <Input label="Full Name" value={fullName} onChange={e => setFullName(e.target.value)} />
          <Input label="Phone" type="tel" value={phone} onChange={e => setPhone(e.target.value)} />
          <Input label="District" value={district} onChange={e => setDistrict(e.target.value)} />
          <div>
            <label className="text-sm font-medium text-gray-300 mb-1 block">Bio</label>
            <textarea
              value={bio}
              onChange={e => setBio(e.target.value)}
              rows={3}
              className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-green-500"
            />
          </div>
          {error && <p className="text-red-400 text-sm">{error}</p>}
          <div className="flex gap-2">
            <Button type="submit" loading={loading} fullWidth>Save</Button>
            <Button type="button" variant="ghost" onClick={() => setEditing(false)} fullWidth>Cancel</Button>
          </div>
        </form>
      )}

      <Button variant="danger" onClick={handleSignOut}>Sign Out</Button>
    </div>
  );
}
