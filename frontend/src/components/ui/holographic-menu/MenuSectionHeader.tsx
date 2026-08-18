import React from 'react';
import { cn } from '../../../utils/cn';
import { Divider } from '../Divider';

/** Primary title chrome for glance menus inside StatusBarSurfaceHost. */
export const MenuSectionHeader: React.FC<{ children: React.ReactNode; className?: string }> = ({
  children,
  className,
}) => (
  <div className={cn(className)}>
    <div className="px-2 py-2 type-heading text-foreground">
      {children}
    </div>
    <Divider variant="accented" className="mx-2" />
  </div>
);
