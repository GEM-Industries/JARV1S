import React, { useMemo } from 'react';

interface HolographicBorderProps {
  width: number;
  height: number;
  className?: string;
  color?: string;
}

export const HolographicBorder: React.FC<HolographicBorderProps> = ({
  width,
  height,
  className = "",
  color = "stroke-brand/40"
}) => {
  if (width === 0 || height === 0) return null;

  const radius = height / 2;
  
  const perimeter = useMemo(() => {
    return (2 * Math.PI * radius) + (2 * (width - (2 * radius)));
  }, [width, radius]);

  const bracketLength = useMemo(() => {
     const circlePerimeter = Math.PI * height;
     const halfCircle = circlePerimeter / 2;
     const baseGap = 24; 
     return Math.max(0, halfCircle - baseGap);
  }, [height]);

  const { strokeDashArray, offset } = useMemo(() => {
    const halfPerimeter = perimeter / 2;
    const dynamicGap = Math.max(0, halfPerimeter - bracketLength);
    const offset = -dynamicGap / 2;
    
    return { 
        strokeDashArray: `${bracketLength} ${dynamicGap}`, 
        offset 
    }; 
  }, [perimeter, bracketLength]);

  return (
    <svg 
      width={width} 
      height={height} 
      className={`absolute inset-0 pointer-events-none overflow-visible ${className}`}
      fill="none"
      strokeWidth="1"
    >
      <path 
        d={`
          M ${width/2} ${height} 
          L ${radius} ${height} 
          A ${radius} ${radius} 0 0 1 ${radius} 0 
          L ${width - radius} 0 
          A ${radius} ${radius} 0 0 1 ${width - radius} ${height} 
          L ${width/2} ${height}
        `}
        strokeLinecap="round" 
        strokeDasharray={strokeDashArray}
        strokeDashoffset={offset} 
        className={`transition-all duration-500 ease-out ${color}`}
      />
    </svg>
  );
};
