import { CloudRainIcon, SunIcon, CloudIcon, CloudSnowIcon, LightningIcon, CaretRightIcon, DropIcon, WindIcon } from '@phosphor-icons/react';
import { WidgetDefinition, BaseWidgetProps } from './types';
import { TacticalButton } from '../../ui/TacticalButton';
import React, { useState } from 'react';
import { cn } from '../../../utils/cn';

interface ForecastDay {
  day: string;
  high: number;
  low: number;
  condition: string;
}

interface WeatherData {
  temp: number;
  condition: string;
  location?: string;
  time?: string;
  precipitation?: string;
  humidity?: string;
  wind?: string;
  forecast?: ForecastDay[];
  attributionUrl?: string;
  attributionLabel?: string;
}

const MAX_FORECAST_DAYS = 7;

const getWeatherIcon = (condition: string, size: number = 80) => {
  const c = condition.toLowerCase();
  if (c.includes('rain')) return <CloudRainIcon size={size} weight="light" className="text-brand drop-shadow-glow-brand" />;
  if (c.includes('cloud')) return <CloudIcon size={size} weight="light" className="text-foreground-muted" />;
  if (c.includes('snow')) return <CloudSnowIcon size={size} weight="light" />;
  if (c.includes('storm')) return <LightningIcon size={size} weight="light" className="text-status-warning" />;
  return <SunIcon size={size} weight="light" className="text-status-warning drop-shadow-glow-warning" />;
};

const WeatherHero: React.FC<WeatherData & BaseWidgetProps> = ({ 
  temp, condition, location, time = "Now",
  precipitation, humidity, wind, forecast,
  attributionUrl, attributionLabel,
}) => {
  const [viewIndex, setViewIndex] = useState(0); // 0=main, 1=details, 2=forecast

  // Fallbacks for data driven values
  const precipValue = precipitation || "0%";
  const humidityValue = humidity || "0%";
  const windValue = wind || "0km/h";

  // Determine max views based on whether forecast data exists
  const maxViews = forecast && forecast.length > 0 ? 3 : 2;
  
  const toggleView = () => {
    setViewIndex((prev) => (prev + 1) % maxViews);
  };

  const getViewTitle = () => {
    switch(viewIndex) {
      case 0: return "Today's Weather";
      case 1: return "Current Details";
      case 2: return "Weekly Forecast";
      default: return "Weather";
    }
  };

  return (
    <div className="flex flex-col h-full justify-between group transition-colors select-none px-5 py-4 overflow-hidden relative">
      {/* Sub-Header (Specific to Weather) */}
      <div className="flex items-center justify-between z-10">
        <div className="flex items-center gap-3">
            <span className="type-heading text-foreground">{getViewTitle()}</span>
            <div className="w-px h-3 flex flex-col opacity-50">
                <div className="h-[3px] bg-surface-highlight" />
                <div className="flex-1 bg-outline" />
            </div>
            <span className="type-meta tabular-nums text-outline">{time}</span>
        </div>
      </div>

      {/* Main Content Area - Sliding Container */}
      <div className="flex-1 relative">
        <div 
          className="absolute inset-0 flex transition-transform duration-500 ease-hologram"
          style={{ transform: `translateX(${-viewIndex * 100}%)` }}
        >
          {/* VIEW 0: Main Weather */}
          <div 
            className={cn(
              "w-full h-full flex items-center justify-start gap-6 shrink-0 transition-all duration-500 ease-hologram",
              viewIndex !== 0 ? "opacity-0 scale-95" : "opacity-100 scale-100"
            )}
          >
            {/* Icon */}
            <div className="relative shrink-0">
                {getWeatherIcon(condition, 72)}
                <div className="absolute -bottom-3 left-3 w-10 h-1 bg-brand/10 blur-md rounded-full" />
            </div>

            {/* Temp & Location Group */}
            <div className="flex flex-col items-start pt-1">
                <div className="flex items-start leading-none -mb-1">
                    <span className="font-display font-medium text-6xl text-foreground tracking-tighter">
                        {Math.round(temp)}
                    </span>
                    <span className="font-display text-2xl text-foreground/90 mt-1.5 ml-1">°C</span>
                </div>
                {location ? <span className="type-label text-outline">{location}</span> : null}
            </div>
          </div>

          {/* VIEW 1: Tactical Details */}
          <div 
            className={cn(
              "w-full h-full flex items-center justify-around shrink-0 px-4 transition-all duration-500 ease-hologram",
              viewIndex !== 1 ? "opacity-0 scale-95" : "opacity-100 scale-100"
            )}
          >
            <div className="flex flex-col items-center gap-1.5">
              <div className="w-10 h-10 rounded-full border border-surface flex items-center justify-center text-brand bg-brand/5">
                <CloudRainIcon size={20} weight="light" />
              </div>
              <span className="type-label-small text-foreground-subtle">Precip</span>
              <span className="text-lg font-display font-medium text-foreground">{precipValue}</span>
            </div>

            <div className="w-px h-10 bg-outline/30" />

            <div className="flex flex-col items-center gap-1.5">
              <div className="w-10 h-10 rounded-full border border-surface flex items-center justify-center text-brand bg-brand/5">
                <DropIcon size={20} weight="light" />
              </div>
              <span className="type-label-small text-foreground-subtle">Humidity</span>
              <span className="text-lg font-display font-medium text-foreground">{humidityValue}</span>
            </div>

            <div className="w-px h-10 bg-outline/30" />

            <div className="flex flex-col items-center gap-1.5">
              <div className="w-10 h-10 rounded-full border border-surface flex items-center justify-center text-brand bg-brand/5">
                <WindIcon size={20} weight="light" />
              </div>
              <span className="type-label-small text-foreground-subtle">Wind</span>
              <span className="text-lg font-display font-medium text-foreground">{windValue}</span>
            </div>
          </div>

          {/* VIEW 2: Weekly Forecast */}
          {forecast && (
            <div 
              className={cn(
                "w-full h-full flex items-center justify-between shrink-0 px-2 transition-all duration-500 ease-hologram",
                viewIndex !== 2 ? "opacity-0 scale-95" : "opacity-100 scale-100"
              )}
            >
              {forecast.slice(0, MAX_FORECAST_DAYS).map((day, i) => (
                <div key={i} className="flex flex-col items-center gap-2">
                  <span className="type-label-small text-outline">{day.day}</span>
                  <div className="my-1 text-foreground-muted">
                    {getWeatherIcon(day.condition, 24)}
                  </div>
                  <div className="flex flex-col items-center leading-tight">
                    <span className="text-base font-display font-medium text-foreground">{Math.round(day.high)}°</span>
                    <span className="type-meta tabular-nums text-foreground-subtle">{Math.round(day.low)}°</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Footer / Details */}
      <div className="mt-1 z-10">
        <div className="flex items-end justify-between">
            <div className="flex flex-col">
                <span className="text-[11px] font-body font-medium text-outline uppercase tracking-wider mb-0.5">
                  Type
                </span>
                <span className="font-body text-base text-foreground tracking-wide leading-normal truncate max-w-[200px]">
                    {condition}
                </span>
                {attributionUrl && attributionLabel && (
                  <a
                    href={attributionUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-1 text-[10px] text-foreground-subtle hover:text-foreground-muted underline"
                  >
                    {attributionLabel}
                  </a>
                )}
            </div>

            <TacticalButton 
              className="w-7 h-7 shrink-0 text-surface-highlight"
              onClick={toggleView}
              active={viewIndex > 0}
            >
                <CaretRightIcon size={18} weight="bold" />
            </TacticalButton>
        </div>
      </div>
    </div>
  );
};

export const WeatherWidget: WidgetDefinition<WeatherData> = {
  Hero: WeatherHero,
  getCompressedConfig: (data) => ({
    icon: <div className="scale-75">{getWeatherIcon(data.condition, 40)}</div>,
    label: `${Math.round(data.temp)}°`,
    labelVariant: 'display',
    width: 'wide'
  })
};
