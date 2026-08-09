import type {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';

import { createTesseraPanelWidget } from './widget';

/**
 * Rank of the tessera panel among the right-sidebar widgets.
 */
const SIDEBAR_RANK = 900;

/**
 * The tessera JupyterLab extension.
 *
 * @remarks
 * Registers the tessera panel in the right sidebar: one live status
 * entry per OAuth server declared in the service configuration. The
 * frontend never reads, stores, or logs an OAuth token; it only
 * consumes the booleans and metadata exposed by the tessera service.
 */
const plugin: JupyterFrontEndPlugin<void> = {
  id: 'tessera:plugin',
  autoStart: true,
  activate: (app: JupyterFrontEnd): void => {
    app.shell.add(createTesseraPanelWidget(), 'right', { rank: SIDEBAR_RANK });
  }
};

export default plugin;
