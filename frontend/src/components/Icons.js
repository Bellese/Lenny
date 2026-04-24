import React from 'react';

const base = (p, extra = {}) => ({
  width: 14,
  height: 14,
  viewBox: '0 0 14 14',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: '1.2',
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  ...extra,
  ...p,
});

export function MeasuresIcon(p) {
  return <svg {...base(p)}><rect x="2" y="2" width="10" height="10" rx="1.5" /><path d="M4.5 5h5M4.5 7h5M4.5 9h3" /></svg>;
}
export function JobsIcon(p) {
  return <svg {...base(p)}><circle cx="7" cy="7" r="5" /><path d="M7 4.3V7l1.8 1.3" /></svg>;
}
export function ResultsIcon(p) {
  return <svg {...base(p)}><path d="M2 12V7.5M6 12V4M10 12V6" /><path d="M2 12h10" /></svg>;
}
export function ValidateIcon(p) {
  return <svg {...base(p)}><path d="M7 2l4 2v3.5c0 2.6-1.8 4.4-4 5-2.2-.6-4-2.4-4-5V4l4-2z" /><path d="M5.5 7l1.2 1.2L9 6" /></svg>;
}
export function SettingsIcon(p) {
  return <svg {...base(p)}><circle cx="7" cy="7" r="2" /><path d="M7 2v1.2M7 10.8V12M11.5 7H10.3M3.7 7H2.5M10.2 3.8l-.85.85M4.65 9.35l-.85.85M10.2 10.2l-.85-.85M4.65 4.65l-.85-.85" /></svg>;
}
export function PatientsIcon(p) {
  return <svg {...base(p)}><circle cx="7" cy="5" r="2.2" /><path d="M2.5 12c.7-2 2.4-3 4.5-3s3.8 1 4.5 3" /></svg>;
}
export function PlusIcon(p) {
  return <svg {...base(p, { strokeWidth: '1.5' })}><path d="M7 3v8M3 7h8" /></svg>;
}
export function SearchIcon(p) {
  return <svg {...base(p)}><circle cx="6.2" cy="6.2" r="3.6" /><path d="M9 9l2.5 2.5" /></svg>;
}
export function ChevronIcon(p) {
  return <svg {...base(p, { strokeWidth: '1.5' })}><path d="M5 3.5L9 7l-4 3.5" /></svg>;
}
export function DotIcon(p) {
  return <svg {...base(p, { fill: 'currentColor', stroke: 'none' })}><circle cx="7" cy="7" r="3" /></svg>;
}
export function CheckIcon(p) {
  return <svg {...base(p, { strokeWidth: '1.5' })}><path d="M3 7.5L6 10.5L11 4.5" /></svg>;
}
export function XIcon(p) {
  return <svg {...base(p, { strokeWidth: '1.4' })}><path d="M4 4l6 6M10 4l-6 6" /></svg>;
}
export function FilterIcon(p) {
  return <svg {...base(p)}><path d="M2.5 3h9l-3.4 4.5V11l-2.2 1.2V7.5L2.5 3z" /></svg>;
}
export function SparkIcon(p) {
  return <svg {...base(p)}><path d="M7 2l1 3.2 3.2 1-3.2 1L7 10.5l-1-3.3-3.2-1 3.2-1z" /></svg>;
}
export function TrashIcon(p) {
  return <svg {...base(p, { strokeWidth: '1.3' })}><path d="M2.5 4h9M5.5 4V2.5h3V4M3.5 4l.5 8h6l.5-8M6 6.5v4M8 6.5v4" /></svg>;
}
export function ViewIcon(p) {
  return <svg {...base(p, { strokeWidth: '1.3' })}><path d="M1 7s2-4 6-4 6 4 6 4-2 4-6 4-6-4-6-4z" /><circle cx="7" cy="7" r="1.5" /></svg>;
}
export function MoonIcon(p) {
  return <svg {...base(p)}><path d="M11 8A5 5 0 016 3a5 5 0 100 10 5 5 0 005-5z" /></svg>;
}
export function SunIcon(p) {
  return <svg {...base(p)}><circle cx="7" cy="7" r="2.5" /><path d="M7 1v1.5M7 11.5V13M1 7h1.5M11.5 7H13M3 3l1 1M10 10l1 1M10 4l1-1M3 10l1-1" /></svg>;
}
