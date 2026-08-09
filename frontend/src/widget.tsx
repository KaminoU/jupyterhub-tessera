import { ReactWidget } from '@jupyterlab/apputils';
import * as React from 'react';

import { TesseraPanel } from './components/TesseraPanel';
import { tesseraIcon } from './icon';

/**
 * Build the sidebar widget hosting the tessera panel.
 *
 * @returns The widget, ready to be added to the application shell.
 *
 * @remarks
 * `ReactWidget` ties the React lifecycle to the Lumino one: disposing
 * the widget unmounts the panel, which tears down its poll timer, its
 * visibility listener, and its in-flight requests.
 */
export function createTesseraPanelWidget(): ReactWidget {
  const widget = ReactWidget.create(<TesseraPanel />);
  widget.id = 'tessera-panel';
  widget.title.icon = tesseraIcon;
  widget.title.caption = 'tessera: OAuth tokens';
  widget.addClass('tessera-PanelWidget');
  return widget;
}
