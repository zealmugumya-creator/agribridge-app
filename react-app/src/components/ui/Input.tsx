/**
 * Copyright (C) 2026 Mugumya Zeal. All rights reserved.
 */

import React from 'react';

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

export function Input({ label, error, className = '', id, ...props }: InputProps) {
  const inputId = id || label?.toLowerCase().replace(/\s+/g, '-');
  return (
    <div className="flex flex-col gap-1">
      {label && (
        <label htmlFor={inputId} className="text-sm font-medium text-gray-300">
          {label}
        </label>
      )}
      <input
        id={inputId}
        className={[
          'bg-gray-800 border rounded-lg px-3 py-2 text-white placeholder-gray-500',
          'focus:outline-none focus:ring-2 focus:ring-green-500',
          error ? 'border-red-500' : 'border-gray-600',
          className,
        ].join(' ')}
        {...props}
      />
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  );
}
