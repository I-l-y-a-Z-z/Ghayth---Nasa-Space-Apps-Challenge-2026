import React from 'react';
import {createRoot} from 'react-dom/client';
import App from './App';
import './styles.css';
import '@agrix/ui/styles.css';
import './upstream.css';
import './integration.css';

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
