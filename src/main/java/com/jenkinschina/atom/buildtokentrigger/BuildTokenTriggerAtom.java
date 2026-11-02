package com.jenkinschina.atom.buildtokentrigger;

import com.jenkinschina.atom.sdk.AtomContext;
import com.jenkinschina.atom.sdk.AtomResult;
import com.jenkinschina.atom.sdk.AtomTask;
import com.jenkinschina.atom.sdk.ErrorType;
import java.io.IOException;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Locale;
import java.util.Map;
import java.util.TreeMap;

/**
 * Triggers a build on a (usually remote) Jenkins through its build token.
 *
 * <p>Port of <em>build-token-trigger</em> 11.v5e2ee5cdb_32a_ ({@code TriggerStep}, step name
 * {@code buildTokenTrigger}). The step is one synchronous outbound POST: it composes
 * {@code <jenkinsUrl>/buildByToken/build|buildWithParameters} with a form body of
 * {@code job=<job>&token=<token>[&delay=<seconds>][&<param>=<value>...]} and reads the {@code Location} header of the
 * answer as the queue URL. There is no job model, no polling and no credential plugin involved, so it is exactly a
 * process-shaped atom.
 *
 * <p>What this atom does not do, and says so instead of guessing: it never follows a redirect (the plugin only
 * inspects the status of the first hop, and following a 302 would re-send the token somewhere else), it has no proxy
 * configuration (the plugin reads {@code Jenkins.proxy}; an atom has no Jenkins to ask), and it requires the token as
 * an input or a bound secret.
 *
 * <p>Deliberate differences from the plugin, all asserted by {@code tools/smoke.sh}: an answer that is neither 404,
 * 302 nor a 2xx is an error that names the status and the first line of the body (the plugin treats every other status
 * as success and then fails with a NullPointerException when there is no {@code Location}); a 2xx without
 * {@code Location} is 40007 (same NPE, reported); 401/403 is 40006 (a wrong or missing build token); 3xx other than
 * 302 is 40008 (the plugin would read {@code Location} of a redirect it never followed); a request timeout is 40009
 * instead of the plugin's connect-timeout-less hang.
 */
public class BuildTokenTriggerAtom implements AtomTask {

    static final int MAX_BODY_SNIPPET = 400;

    @Override
    public AtomResult run(AtomContext context) throws Exception {
        String rawUrl = context.getParam("jenkinsUrl", "").trim();
        String job = context.getParam("job", "").trim();
        String token = firstNonEmpty(context.getParam("token", ""), context.getSecret("token"));
        String rawDelay = context.getParam("delay", "").trim();
        boolean ignoreMissing = context.getBooleanParam("ignoreMissing", false);
        boolean ignoreUnqueued = context.getBooleanParam("ignoreUnqueued", false);
        int timeoutSeconds = Math.max(1, Math.min(context.getIntParam("timeoutSeconds", 60), 600));

        if (rawUrl.isEmpty()) {
            return AtomResult.error(ErrorType.USER, 40001,
                    "jenkinsUrl is required (the Jenkins that owns the job, for example https://ci.example.com)");
        }
        if (job.isEmpty()) {
            return AtomResult.error(ErrorType.USER, 40001, "job is required (for example folder/my-job)");
        }
        if (token == null || token.isBlank()) {
            return AtomResult.error(ErrorType.USER, 40001,
                    "token is required: a Jenkins build token (Job configuration -> Build Triggers -> Trigger builds "
                            + "remotely), not a user password");
        }

        String base;
        try {
            base = normalizeUrl(rawUrl);
            URI uri = new URI(base);
            if (!"http".equalsIgnoreCase(uri.getScheme()) && !"https".equalsIgnoreCase(uri.getScheme())) {
                return AtomResult.error(ErrorType.USER, 40002, "jenkinsUrl must be http or https: " + rawUrl);
            }
            if (uri.getHost() == null) {
                return AtomResult.error(ErrorType.USER, 40002, "jenkinsUrl has no host: " + rawUrl);
            }
            if (uri.getQuery() != null || uri.getFragment() != null) {
                return AtomResult.error(ErrorType.USER, 40002,
                        "jenkinsUrl must not carry a query or fragment: " + rawUrl);
            }
        } catch (URISyntaxException e) {
            return AtomResult.error(ErrorType.USER, 40002, "jenkinsUrl is not a URL: " + rawUrl);
        }

        TreeMap<String, String> parameters = new TreeMap<>();
        for (String line : context.getParam("parameters", "").split("\\R")) {
            String trimmed = line.trim();
            if (trimmed.isEmpty() || trimmed.startsWith("#")) {
                continue;
            }
            int eq = trimmed.indexOf('=');
            if (eq <= 0) {
                return AtomResult.error(ErrorType.USER, 40001,
                        "parameters must be KEY=VALUE per line; got '" + trimmed + "'");
            }
            parameters.put(trimmed.substring(0, eq).trim(), trimmed.substring(eq + 1));
        }

        // the plugin's newInstance() turns a negative or unparsable delay into "no delay" instead of failing;
        // kept, because a job that passes ${QUIET_PERIOD} with an empty default must keep working
        Integer delay = null;
        if (!rawDelay.isEmpty()) {
            try {
                int parsed = Integer.parseInt(rawDelay);
                if (parsed >= 0) {
                    delay = parsed;
                }
            } catch (NumberFormatException e) {
                delay = null;
            }
        }

        String jobPath = "/job/" + stripLeadingSlash(stripTrailingSlash(job)).replace("/", "/job/");
        String jobUrl = base + jobPath;
        String triggerUrl = base + "/buildByToken" + (parameters.isEmpty() ? "/build" : "/buildWithParameters");
        byte[] body = body(job, token, delay, parameters).getBytes(StandardCharsets.UTF_8);

        context.log("Triggering " + jobUrl);
        context.log("POST " + triggerUrl + " (" + body.length + " bytes)");

        HttpClient client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(timeoutSeconds))
                .followRedirects(HttpClient.Redirect.NEVER)
                .build();
        HttpRequest request = HttpRequest.newBuilder(URI.create(triggerUrl))
                .timeout(Duration.ofSeconds(timeoutSeconds))
                .header("Content-Type", "application/x-www-form-urlencoded")
                .header("Accept", "*/*")
                .POST(HttpRequest.BodyPublishers.ofByteArray(body))
                .build();

        HttpResponse<String> response;
        try {
            response = client.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        } catch (HttpTimeoutException e) {
            return AtomResult.error(ErrorType.THIRD_PART, 40009,
                    "no answer from " + triggerUrl + " within " + timeoutSeconds + "s");
        } catch (IOException e) {
            return AtomResult.error(ErrorType.THIRD_PART, 40002,
                    "could not reach " + triggerUrl + ": " + e.getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return AtomResult.error(ErrorType.SYSTEM, 40002, "interrupted while calling " + triggerUrl);
        }

        int status = response.statusCode();
        String location = response.headers().firstValue("location").orElse(null);
        String snippet = snippet(response.body());
        context.log("Trigger returned HTTP/" + status
                + (location == null ? "" : " Location: " + location));

        if (status == 404) {
            if (ignoreMissing) {
                context.log("Job " + job + " not found; ignoreMissing is set");
                return queued(context, "missing", "about:missing", status, jobUrl, triggerUrl, "");
            }
            return AtomResult.error(ErrorType.THIRD_PART, 40003,
                    "job " + job + " was not found on " + base + " using the supplied build token"
                            + (snippet.isEmpty() ? "" : ": " + snippet));
        }
        if (status == 302) {
            if (ignoreUnqueued) {
                context.log("Job " + job + " not queued; ignoreUnqueued is set");
                return queued(context, "unqueued", "about:unqueued", status, jobUrl, triggerUrl, location);
            }
            return AtomResult.error(ErrorType.THIRD_PART, 40004,
                    "job " + job + " on " + base + " was not accepted into the build queue"
                            + (location == null ? "" : " (redirected to " + location + ")"));
        }
        if (status >= 300 && status < 400) {
            return AtomResult.error(ErrorType.THIRD_PART, 40008,
                    "unexpected redirect HTTP/" + status + " from " + triggerUrl
                            + (location == null ? "" : " to " + location)
                            + "; the build token is not re-sent across redirects");
        }
        if (status == 401 || status == 403) {
            return AtomResult.error(ErrorType.THIRD_PART, 40006,
                    "the build token was rejected (HTTP/" + status + ") by " + base
                            + (snippet.isEmpty() ? "" : ": " + snippet));
        }
        if (status < 200 || status >= 300) {
            return AtomResult.error(ErrorType.THIRD_PART, 40005,
                    "unexpected HTTP/" + status + " from " + triggerUrl
                            + (snippet.isEmpty() ? "" : ": " + snippet));
        }
        if (location == null || location.isBlank()) {
            return AtomResult.error(ErrorType.THIRD_PART, 40007,
                    "HTTP/" + status + " from " + triggerUrl + " carried no Location header, so the queued build "
                            + "cannot be identified" + (snippet.isEmpty() ? "" : ": " + snippet));
        }
        String queueUrl = location.startsWith("/") ? base + location : location;
        context.log("Job queued as " + queueUrl);
        return queued(context, "queued", queueUrl, status, jobUrl, triggerUrl, location);
    }

    /** The form body, in the plugin's order: job, token, delay, then the parameters sorted by key. */
    static String body(String job, String token, Integer delay, Map<String, String> parameters) {
        StringBuilder data = new StringBuilder();
        data.append("job=").append(encode(job));
        data.append("&token=").append(encode(token));
        if (delay != null && delay >= 0) {
            data.append("&delay=").append(delay);
        }
        for (Map.Entry<String, String> entry : parameters.entrySet()) {
            data.append("&").append(encode(entry.getKey())).append("=").append(encode(entry.getValue()));
        }
        return data.toString();
    }

    /**
     * {@code TriggerCredentialsImpl.normalizeUrl}: lowercase the host, drop the port a scheme implies, drop one
     * trailing slash. Two runs with {@code HTTP://CI.EXAMPLE.COM:80/} and {@code http://ci.example.com} must produce the
     * same request, because the plugin compares the credential's URL with the step's after this normalization.
     */
    static String normalizeUrl(String url) throws URISyntaxException {
        String value = url == null ? "" : url;
        URI uri = new URI(value).normalize();
        String scheme = uri.getScheme();
        if ("http".equals(scheme) || "https".equals(scheme)) {
            String host = uri.getHost() == null ? null : uri.getHost().toLowerCase(Locale.ENGLISH);
            int port = uri.getPort();
            if (("http".equals(scheme) && port == 80) || ("https".equals(scheme) && port == 443)) {
                port = -1;
            }
            value = new URI(scheme, uri.getUserInfo(), host, port, uri.getPath(), uri.getQuery(), uri.getFragment())
                    .toASCIIString();
        }
        return value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
    }

    private static AtomResult queued(AtomContext context, String outcome, String queueUrl, int status,
                                     String jobUrl, String triggerUrl, String location) {
        return AtomResult.success("build token trigger: " + outcome + " " + queueUrl)
                .data("outcome", outcome)
                .data("queued", "queued".equals(outcome) ? "true" : "false")
                .data("queueUrl", queueUrl)
                .data("httpCode", Integer.toString(status))
                .data("jobUrl", jobUrl)
                .data("triggerUrl", triggerUrl)
                .data("location", location == null ? "" : location);
    }

    private static String encode(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8);
    }

    private static String snippet(String body) {
        if (body == null) {
            return "";
        }
        String flat = body.replaceAll("\\s+", " ").trim();
        return flat.length() <= MAX_BODY_SNIPPET ? flat : flat.substring(0, MAX_BODY_SNIPPET) + "...";
    }

    private static String stripLeadingSlash(String value) {
        return value.startsWith("/") ? value.substring(1) : value;
    }

    private static String stripTrailingSlash(String value) {
        return value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
    }

    private static String firstNonEmpty(String first, String second) {
        if (first != null && !first.isEmpty()) {
            return first;
        }
        return second == null ? "" : second;
    }
}
