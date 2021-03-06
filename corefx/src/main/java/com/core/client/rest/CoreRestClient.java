package com.core.client.rest;

import com.core.Controller;
import com.core.client.ICoreClient;
import com.core.data.*;
import com.core.utils.WebUtils;
import com.core.websocket.CoreWebSocket;
import lombok.Data;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.io.File;
import java.io.IOException;
import java.net.URISyntaxException;
import java.util.*;

@Data
public class CoreRestClient implements ICoreClient {
    private static final Logger logger = LogManager.getLogger();
    private String address;
    private int port;
    private Integer sessionId;
    private SessionState sessionState;
    private CoreWebSocket coreWebSocket;

    @Override
    public void setConnection(String address, int port) {
        this.address = address;
        this.port = port;
    }

    @Override
    public boolean isLocalConnection() {
        return address.equals("127.0.0.1") || address.equals("localhost");
    }

    @Override
    public Integer currentSession() {
        return sessionId;
    }

    @Override
    public void updateState(SessionState state) {
        sessionState = state;
    }

    @Override
    public void updateSession(Integer sessionId) {
        this.sessionId = sessionId;
    }

    private String getUrl(String path) {
        return String.format("http://%s:%s/%s", address, port, path);
    }

    @Override
    public SessionOverview createSession() throws IOException {
        String url = getUrl("sessions");
        return WebUtils.post(url, SessionOverview.class);
    }

    @Override
    public boolean deleteSession(Integer sessionId) throws IOException {
        String path = String.format("sessions/%s", sessionId);
        String url = getUrl(path);
        return WebUtils.delete(url);
    }

    public Map<String, List<String>> getServices() throws IOException {
        String url = getUrl("services");
        GetServices getServices = WebUtils.getJson(url, GetServices.class);
        return getServices.getGroups();
    }

    @Override
    public Session getSession(Integer sessionId) throws IOException {
        String path = String.format("sessions/%s", sessionId);
        String url = getUrl(path);
        return WebUtils.getJson(url, Session.class);
    }

    @Override
    public List<SessionOverview> getSessions() throws IOException {
        String url = getUrl("sessions");
        GetSessions getSessions = WebUtils.getJson(url, GetSessions.class);
        return getSessions.getSessions();
    }

    @Override
    public boolean start(Collection<CoreNode> nodes, Collection<CoreLink> links, List<Hook> hooks) throws IOException {
        boolean result = setState(SessionState.DEFINITION);
        if (!result) {
            return false;
        }

        result = setState(SessionState.CONFIGURATION);
        if (!result) {
            return false;
        }

        for (Hook hook : hooks) {
            if (!createHook(hook)) {
                return false;
            }
        }

        for (CoreNode node : nodes) {
            // must pre-configure wlan nodes, if not already
            if (node.getNodeType().getValue() == NodeType.WLAN) {
                WlanConfig config = getWlanConfig(node);
                setWlanConfig(node, config);
            }

            if (!createNode(node)) {
                return false;
            }
        }

        for (CoreLink link : links) {
            if (!createLink(link)) {
                return false;
            }
        }

        return setState(SessionState.INSTANTIATION);
    }

    @Override
    public boolean stop() throws IOException {
        return setState(SessionState.SHUTDOWN);
    }

    @Override
    public boolean setState(SessionState state) throws IOException {
        String url = getUrl(String.format("sessions/%s/state", sessionId));
        Map<String, Integer> data = new HashMap<>();
        data.put("state", state.getValue());
        boolean result = WebUtils.putJson(url, data);

        if (result) {
            sessionState = state;
        }
        return result;
    }

    private boolean uploadFile(File file) throws IOException {
        String url = getUrl("upload");
        return WebUtils.postFile(url, file);
    }

    @Override
    public boolean startThroughput(Controller controller) throws IOException {
        String url = getUrl("throughput/start");
        return WebUtils.putJson(url);
    }

    @Override
    public boolean stopThroughput() throws IOException {
        String url = getUrl("throughput/stop");
        return WebUtils.putJson(url);
    }

    @Override
    public Map<String, List<String>> getDefaultServices() throws IOException {
        String url = getUrl(String.format("sessions/%s/services/default", sessionId));
        GetDefaultServices getDefaultServices = WebUtils.getJson(url, GetDefaultServices.class);
        return getDefaultServices.getDefaults();
    }

    @Override
    public boolean setDefaultServices(Map<String, Set<String>> defaults) throws IOException {
        String url = getUrl(String.format("sessions/%s/services/default", sessionId));
        return WebUtils.postJson(url, defaults);
    }

    @Override
    public CoreService getService(CoreNode node, String serviceName) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/services/%s", sessionId, node.getId(), serviceName));
        return WebUtils.getJson(url, CoreService.class);
    }

    @Override
    public boolean setService(CoreNode node, String serviceName, CoreService service) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/services/%s", sessionId, node.getId(), serviceName));
        return WebUtils.putJson(url, service);
    }

    @Override
    public String getServiceFile(CoreNode node, String serviceName, String fileName) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/services/%s/file", sessionId, node.getId(),
                serviceName));
        Map<String, String> args = new HashMap<>();
        args.put("file", fileName);
        return WebUtils.getJson(url, String.class, args);
    }

    @Override
    public boolean startService(CoreNode node, String serviceName) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/services/%s/start", sessionId, node.getId(),
                serviceName));
        return WebUtils.putJson(url);
    }

    @Override
    public boolean stopService(CoreNode node, String serviceName) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/services/%s/stop", sessionId, node.getId(),
                serviceName));
        return WebUtils.putJson(url);
    }

    @Override
    public boolean restartService(CoreNode node, String serviceName) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/services/%s/restart", sessionId, node.getId(),
                serviceName));
        return WebUtils.putJson(url);
    }

    @Override
    public boolean validateService(CoreNode node, String serviceName) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/services/%s/validate", sessionId, node.getId(),
                serviceName));
        return WebUtils.putJson(url);
    }

    @Override
    public boolean setServiceFile(CoreNode node, String serviceName, ServiceFile serviceFile) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/services/%s/file", sessionId, node.getId(),
                serviceName));
        return WebUtils.putJson(url, serviceFile);
    }

    @Override
    public List<String> getEmaneModels() throws IOException {
        String url = getUrl(String.format("sessions/%s/emane/models", sessionId));
        GetEmaneModels getEmaneModels = WebUtils.getJson(url, GetEmaneModels.class);
        return getEmaneModels.getModels();
    }

    @Override
    public List<ConfigGroup> getEmaneModelConfig(Integer id, String model) throws IOException {
        String url = getUrl(String.format("sessions/%s/emane/model/config", sessionId));
        Map<String, String> args = new HashMap<>();
        args.put("node", id.toString());
        args.put("name", model);
        GetConfig getConfig = WebUtils.getJson(url, GetConfig.class, args);
        return getConfig.getGroups();
    }

    @Override
    public List<ConfigGroup> getEmaneConfig(CoreNode node) throws IOException {
        String url = getUrl(String.format("sessions/%s/emane/config", sessionId));
        Map<String, String> args = new HashMap<>();
        args.put("node", node.getId().toString());
        GetConfig getConfig = WebUtils.getJson(url, GetConfig.class, args);
        return getConfig.getGroups();
    }

    @Override
    public boolean setEmaneConfig(CoreNode node, List<ConfigOption> options) throws IOException {
        String url = getUrl(String.format("sessions/%s/emane/config", sessionId));
        SetEmaneConfig setEmaneConfig = new SetEmaneConfig();
        setEmaneConfig.setNode(node.getId());
        setEmaneConfig.setValues(options);
        return WebUtils.putJson(url, setEmaneConfig);
    }

    @Override
    public boolean setEmaneModelConfig(Integer id, String model, List<ConfigOption> options) throws IOException {
        String url = getUrl(String.format("sessions/%s/emane/model/config", sessionId));
        SetEmaneModelConfig setEmaneModelConfig = new SetEmaneModelConfig();
        setEmaneModelConfig.setNode(id);
        setEmaneModelConfig.setName(model);
        setEmaneModelConfig.setValues(options);
        return WebUtils.putJson(url, setEmaneModelConfig);
    }

    @Override
    public boolean isRunning() {
        return sessionState == SessionState.RUNTIME;
    }

    @Override
    public void saveSession(File file) throws IOException {
        String path = String.format("sessions/%s/xml", sessionId);
        String url = getUrl(path);
        WebUtils.getFile(url, file);
    }

    @Override
    public SessionOverview openSession(File file) throws IOException {
        String url = getUrl("sessions/xml");
        return WebUtils.postFile(url, file, SessionOverview.class);
    }

    @Override
    public List<ConfigGroup> getSessionConfig() throws IOException {
        String url = getUrl(String.format("sessions/%s/options", sessionId));
        GetConfig getConfig = WebUtils.getJson(url, GetConfig.class);
        return getConfig.getGroups();
    }

    @Override
    public boolean setSessionConfig(List<ConfigOption> configOptions) throws IOException {
        String url = getUrl(String.format("sessions/%s/options", sessionId));
        SetConfig setConfig = new SetConfig(configOptions);
        return WebUtils.putJson(url, setConfig);
    }

    @Override
    public LocationConfig getLocationConfig() throws IOException {
        String url = getUrl(String.format("sessions/%s/location", sessionId));
        return WebUtils.getJson(url, LocationConfig.class);
    }

    @Override
    public boolean setLocationConfig(LocationConfig config) throws IOException {
        String url = getUrl(String.format("sessions/%s/location", sessionId));
        return WebUtils.putJson(url, config);
    }

    @Override
    public String nodeCommand(CoreNode node, String command) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/command", sessionId, node.getId()));
        return WebUtils.putJson(url, command, String.class);
    }

    @Override
    public boolean createNode(CoreNode node) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes", sessionId));
        return WebUtils.postJson(url, node);
    }


    @Override
    public boolean editNode(CoreNode node) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s", sessionId, node.getId()));
        return WebUtils.putJson(url, node);
    }

    @Override
    public boolean deleteNode(CoreNode node) throws IOException {
        String url = getUrl(String.format("/sessions/%s/nodes/%s", sessionId, node.getId()));
        return WebUtils.delete(url);
    }

    @Override
    public boolean createLink(CoreLink link) throws IOException {
        String url = getUrl(String.format("sessions/%s/links", sessionId));
        return WebUtils.postJson(url, link);
    }

    @Override
    public boolean editLink(CoreLink link) throws IOException {
        String url = getUrl(String.format("sessions/%s/links", sessionId));
        return WebUtils.putJson(url, link);
    }

    @Override
    public boolean createHook(Hook hook) throws IOException {
        String url = getUrl(String.format("sessions/%s/hooks", sessionId));
        return WebUtils.postJson(url, hook);
    }

    @Override
    public List<Hook> getHooks() throws IOException {
        String url = getUrl(String.format("sessions/%s/hooks", sessionId));
        GetHooks getHooks = WebUtils.getJson(url, GetHooks.class);
        return getHooks.getHooks();
    }

    @Override
    public WlanConfig getWlanConfig(CoreNode node) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/wlan", sessionId, node.getId()));
        return WebUtils.getJson(url, WlanConfig.class);
    }

    @Override
    public boolean setWlanConfig(CoreNode node, WlanConfig config) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/wlan", sessionId, node.getId()));
        return WebUtils.putJson(url, config);
    }

    @Override
    public String getTerminalCommand(CoreNode node) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/terminal", sessionId, node.getId()));
        return WebUtils.getJson(url, String.class);
    }

    @Override
    public boolean setMobilityConfig(CoreNode node, MobilityConfig config) throws IOException {
        boolean uploaded = uploadFile(config.getScriptFile());
        if (!uploaded) {
            throw new IOException("failed to upload mobility script");
        }

        String url = getUrl(String.format("sessions/%s/nodes/%s/mobility", sessionId, node.getId()));
        config.setFile(config.getScriptFile().getName());
        return WebUtils.postJson(url, config);
    }

    @Override
    public Map<Integer, MobilityConfig> getMobilityConfigs() throws IOException {
        String url = getUrl(String.format("sessions/%s/mobility/configs", sessionId));
        GetMobilityConfigs getMobilityConfigs = WebUtils.getJson(url, GetMobilityConfigs.class);
        return getMobilityConfigs.getConfigurations();
    }

    @Override
    public MobilityConfig getMobilityConfig(CoreNode node) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/mobility", sessionId, node.getId()));
        return WebUtils.getJson(url, MobilityConfig.class);
    }

    @Override
    public boolean mobilityAction(CoreNode node, String action) throws IOException {
        String url = getUrl(String.format("sessions/%s/nodes/%s/mobility/%s", sessionId, node.getId(), action));
        return WebUtils.putJson(url);
    }

    @Override
    public void setupEventHandlers(Controller controller) throws IOException {
        coreWebSocket.stop();
        coreWebSocket = new CoreWebSocket(controller);
        try {
            coreWebSocket.start(address, port);
        } catch (URISyntaxException ex) {
            throw new IOException("error starting web socket", ex);
        }
    }
}
