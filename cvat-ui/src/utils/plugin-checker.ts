// Copyright (C) 2020 Intel Corporation
//
// SPDX-License-Identifier: MIT

import getCore from 'cvat-core-wrapper';
import { SupportedPlugins } from 'reducers/interfaces';
import isReachable from './url-checker';

const core = getCore();

// Easy plugin checker to understand what plugins supports by a server
class PluginChecker {
    public static async check(plugin: SupportedPlugins): Promise<boolean> {
        const serverHost = core.config.backendAPI.slice(0, -7);

        switch (plugin) {
            case SupportedPlugins.GIT_INTEGRATION: {
                return isReachable(`${serverHost}/git/repository/meta/get`, 'OPTIONS');
            }
            case SupportedPlugins.ANALYTICS: {
                return isReachable(`${serverHost}/analytics/app/kibana`, 'GET');
            }
            default:
                return false;
        }
    }
}

export default PluginChecker;
