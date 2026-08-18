import React from 'react';

export const MenuContext = React.createContext<{ onClose: () => void }>({ onClose: () => {} });
